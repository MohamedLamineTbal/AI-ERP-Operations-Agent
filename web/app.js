(function () {
  "use strict";

  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const fmt = (n) => Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const round2 = (n) => Math.round(n * 100) / 100;
  const now = () => new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const TODAY = midnight(new Date());
  const parseDate = (iso) => new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
  const daysFrom = (iso) => Math.round((midnight(parseDate(iso)) - TODAY) / 86400000);
  const dateIn = (days) => { const d = new Date(TODAY); d.setDate(d.getDate() + days); return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }); };
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const frag = (...nodes) => { const f = document.createDocumentFragment(); nodes.forEach((n) => n && f.appendChild(n)); return f; };

  /* Server */
  async function errorText(res) {
    try {
      const body = await res.json();
      if (typeof body.detail === "string") return body.detail;
    } catch (e) { /* no JSON body */ }
    return res.status === 422 ? "Type a question first." : "Something went wrong.";
  }
  async function fetchJSON(url, options = {}) {
    const res = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
    if (!res.ok) throw new Error(await errorText(res));
    return res.json();
  }

  /* Records loaded from the database */
  const DB = { customers: [], products: [], orders: [], invoices: [], purchaseOrders: [] };
  let recordsError = null;
  async function loadRecords() {
    try {
      const raw = await fetchJSON("/api/records");
      DB.customers = raw.customers.map((c) => ({ ...c, since: -daysFrom(c.createdAt) }));
      DB.products = raw.products;
      DB.orders = raw.orders.map((o) => ({ ...o, daysAgo: -daysFrom(o.createdAt) }));
      DB.invoices = raw.invoices.map((i) => ({ ...i, dueIn: daysFrom(i.dueDate), reminder: i.reminder ? dateIn(daysFrom(i.reminder)) : null }));
      DB.purchaseOrders = raw.purchaseOrders.map((po) => ({ ...po, daysAgo: -daysFrom(po.createdAt) }));
      recordsError = null;
    } catch (e) {
      recordsError = "Couldn't load the records. Check that the server is running, then reload the page.";
    }
  }
  const byId = (list, id) => list.find((r) => r.id === id);
  const customer = (id) => byId(DB.customers, id);
  const product = (id) => byId(DB.products, id);
  const order = (id) => byId(DB.orders, id);
  const invoice = (id) => byId(DB.invoices, id);
  const invoiceForOrder = (orderId) => DB.invoices.find((i) => i.orderId === orderId);
  const companyOf = (customerId) => (customer(customerId) || { company: "" }).company;
  const isLow = (p) => p.stock < p.thr;
  const daysLate = (inv) => Math.max(0, -inv.dueIn);
  const customerOrders = (c) => DB.orders.filter((o) => o.customerId === c.id);
  const customerInvoices = (c) => DB.invoices.filter((i) => i.customerId === c.id);
  const spent = (c) => round2(customerOrders(c).filter((o) => o.status !== "cancelled").reduce((s, o) => s + o.total, 0));
  const overdueOf = (c) => round2(customerInvoices(c).filter((i) => i.status === "overdue").reduce((s, i) => s + i.amount, 0));
  function productSales() {
    const sales = new Map();
    DB.orders.filter((o) => o.status !== "cancelled").forEach((o) => o.items.forEach((it) => {
      const s = sales.get(it.productId) || { units: 0, revenue: 0 };
      s.units += it.qty;
      s.revenue = round2(s.revenue + it.qty * it.unitPrice);
      sales.set(it.productId, s);
    }));
    return sales;
  }

  /* Building blocks */
  function table(head, rows, { num = [], foot, narrowHide = [], nowrap = [] } = {}) {
    const wrap = el("div", "ledger-wrap");
    const t = el("table", "ledger");
    const cls = (i) => [num.includes(i) ? "num" : "", narrowHide.includes(i) ? "hide-narrow" : "", nowrap.includes(i) ? "nowrap" : ""].join(" ").trim();
    const hr = t.createTHead().insertRow();
    head.forEach((h, i) => { const th = el("th", cls(i), h); th.scope = "col"; hr.appendChild(th); });
    const tb = t.createTBody();
    rows.forEach((r) => {
      const tr = tb.insertRow();
      r.forEach((c, i) => { const td = tr.insertCell(); td.className = cls(i); if (c instanceof Node) td.appendChild(c); else td.textContent = c; });
    });
    if (foot) { const tr = t.createTFoot().insertRow(); foot.forEach((c, i) => { const td = tr.insertCell(); td.className = cls(i); td.textContent = c; }); }
    wrap.appendChild(t);
    return wrap;
  }
  function resultTable(rows) {
    if (!rows || !rows.length) return null;
    const keys = Object.keys(rows[0]);
    const numeric = (v) => typeof v === "number" || (typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v));
    const num = keys.map((k, i) => (rows.every((r) => r[k] == null || numeric(r[k])) ? i : -1)).filter((i) => i >= 0);
    const label = (k) => cap(k.replace(/_/g, " ").replace(/\bid$/, "ID"));
    const show = (v) => (v == null ? "" : numeric(v) && String(v).includes(".") ? fmt(v) : String(v));
    return table(keys.map(label), rows.map((r) => keys.map((k) => show(r[k]))), { num });
  }
  function beforeAfter(before, after) {
    const s = el("span");
    s.append(el("span", "was", before), document.createTextNode(" → "), el("b", null, after));
    return s;
  }
  function toggle(label, content) {
    const btn = el("button", "linkbtn", label);
    btn.type = "button";
    btn.setAttribute("aria-expanded", "false");
    content.hidden = true;
    btn.addEventListener("click", () => { content.hidden = !content.hidden; btn.setAttribute("aria-expanded", String(!content.hidden)); });
    return btn;
  }
  const withView = (label, node) => frag(toggle(label, node), node);
  const TONE = { pending: "warn", processing: "info", shipped: "info", completed: "good", cancelled: "muted", overdue: "bad", paid: "good", sent: "info", draft: "muted", ordered: "info", received: "good", "below reorder level": "bad", "in stock": "good" };
  const statusPill = (s) => el("span", "status " + (TONE[s] || "muted"), cap(s));
  const CHECK_SVG = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  function answer({ text, body = [], checked = false }) {
    const a = el("div", "answer");
    const paragraphs = String(text || "").split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
    a.appendChild(el("p", "lead", paragraphs.shift() || ""));
    paragraphs.forEach((p) => a.appendChild(el("p", null, p)));
    body.filter(Boolean).forEach((b) => a.appendChild(b));
    if (checked) {
      const f = el("div", "answer-foot");
      const c = el("span", "checked");
      c.innerHTML = CHECK_SVG;
      c.append("Checked against the result");
      f.appendChild(c);
      a.appendChild(f);
    }
    return a;
  }

  /* Slips waiting for approval */
  const pending = [];
  function renderPending() {
    const list = document.getElementById("pending-list");
    list.replaceChildren();
    pending.forEach((p) => {
      const b = el("button", "pending-btn", p.title);
      b.type = "button";
      b.addEventListener("click", () => focusSlip(p.node));
      list.appendChild(b);
    });
    const pill = document.getElementById("pending-pill");
    pill.hidden = !pending.length;
    pill.textContent = `${pending.length} waiting`;
  }
  function focusSlip(node) {
    go("desk");
    node.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    const btn = node.querySelector(".btn-approve");
    if (btn) btn.focus({ preventScroll: true });
  }
  document.getElementById("pending-pill").addEventListener("click", () => pending[0] && focusSlip(pending[0].node));

  /* Turn an approval request from the agent into a slip */
  function slipConfig(actions, totalCost) {
    const n = actions.length;
    const tools = new Set(actions.map((a) => a.tool_name));
    const tool = tools.size === 1 ? actions[0].tool_name : null;
    const args = actions.map((a) => a.arguments);

    if (tool === "create_purchase_order") {
      const unknown = actions.some((a) => a.line_cost == null);
      const rows = actions.map((a) => {
        const p = product(a.arguments.product_id);
        const qty = a.arguments.quantity;
        return [p ? p.name : `Product ${a.arguments.product_id}`, p ? String(p.stock) : "", String(qty), p ? String(p.stock + qty) : "", a.unit_cost != null ? fmt(a.unit_cost) : "Unknown", a.line_cost != null ? fmt(a.line_cost) : "Unknown"];
      });
      return {
        title: n === 1 ? `Create a purchase order for ${args[0].quantity} units?` : `Create ${n} purchase orders?`,
        head: ["Product", "In stock", "Order", "Stock after", "Unit cost", "Cost"],
        rows,
        tableOpts: { num: [1, 2, 3, 4, 5], narrowHide: [3, 4], foot: n > 1 && totalCost != null ? ["Total", "", "", "", "", fmt(totalCost)] : null },
        headsUp: unknown ? "At least one product wasn't found, so its cost is unknown and approving will fail." : null,
        terms: n === 1 ? "The cost is the supplier cost on file today. The order is created as a draft." : `Costs are the supplier cost on file today. All ${n} orders are created together: if one fails, none are created.`,
        approveLabel: totalCost != null ? `Approve ${plural(n, "order")}, ${fmt(totalCost)}` : `Approve ${plural(n, "order")}`,
        describe: (results) => {
          const ids = results.map((r) => r.id);
          const text = ids.length === 1 ? `Done. Created draft purchase order #${ids[0]}.` : `Done. Created ${ids.length} draft purchase orders, #${ids[0]} to #${ids[ids.length - 1]}.`;
          const view = table(["Order", "Product", "Quantity", "Cost"], results.map((r) => ["#" + r.id, (product(r.product_id) || { name: "" }).name, String(r.quantity), fmt(round2(r.quantity * Number(r.unit_cost)))]), { num: [2, 3] });
          return { text, detail: withView(`View the ${plural(ids.length, "draft order")}`, view) };
        },
      };
    }

    if (tool === "cancel_order" || tool === "update_order_status") {
      const cancelling = tool === "cancel_order";
      const rows = args.map((a) => {
        const o = order(a.order_id);
        const next = cancelling ? "cancelled" : a.new_status;
        return ["#" + a.order_id, o ? companyOf(o.customerId) : "", o ? fmt(o.total) : "", o ? beforeAfter(o.status, next) : next];
      });
      let headsUp = null;
      if (n === 1) {
        const o = order(args[0].order_id);
        const inv = o && invoiceForOrder(o.id);
        if (!o) headsUp = `Order ${args[0].order_id} wasn't found, so approving will fail.`;
        else if (o.status === "cancelled") headsUp = `Order ${o.id} is cancelled, so approving will fail.`;
        else if (cancelling && (o.status === "shipped" || o.status === "completed")) headsUp = `Order ${o.id} is ${o.status}. Shipped and completed orders can't be cancelled, so approving will fail.`;
        else if (cancelling && inv && inv.status === "overdue") headsUp = `Invoice #${inv.id} for ${fmt(inv.amount)} is overdue on this order. Cancelling the order doesn't cancel or change the invoice.`;
        else if (cancelling && inv && inv.status === "paid") headsUp = `Invoice #${inv.id} for ${fmt(inv.amount)} is already paid. Cancelling the order doesn't refund it.`;
        else if (cancelling && inv && inv.status === "sent") headsUp = `Invoice #${inv.id} for ${fmt(inv.amount)} has been sent for this order. Cancelling the order doesn't change the invoice.`;
      }
      return {
        title: cancelling ? (n === 1 ? `Cancel order ${args[0].order_id}?` : `Cancel ${n} orders?`) : (n === 1 ? `Change order ${args[0].order_id} to ${args[0].new_status}?` : `Change ${n} orders?`),
        head: ["Order", "Customer", "Total", "Status"],
        rows,
        tableOpts: { num: [2] },
        headsUp,
        terms: cancelling ? "The cancellation reason is saved on the order." : "Only the status changes.",
        approveLabel: cancelling ? (n === 1 ? "Approve cancellation" : `Approve ${n} cancellations`) : (n === 1 ? "Approve status change" : `Approve ${n} status changes`),
        describe: (results) => ({
          text: results.length === 1 ? `Done. Order ${results[0].id} is now ${results[0].status}.` : `Done. Updated ${plural(results.length, "order")}.`,
        }),
      };
    }

    if (tool === "mark_invoice_reminder_sent") {
      const rows = args.map((a) => {
        const inv = invoice(a.invoice_id);
        return ["#" + a.invoice_id, inv ? companyOf(inv.customerId) : "", inv ? cap(inv.status) : "", inv ? fmt(inv.amount) : ""];
      });
      const inv = n === 1 ? invoice(args[0].invoice_id) : null;
      return {
        title: n === 1 ? `Record a reminder for invoice ${args[0].invoice_id}?` : `Record ${n} invoice reminders?`,
        head: ["Invoice", "Customer", "Status", "Amount"],
        rows,
        tableOpts: { num: [3] },
        headsUp: inv && inv.status === "paid" ? `Invoice ${inv.id} is already paid. A reminder probably isn't needed.` : inv && inv.reminder ? `A reminder for invoice ${inv.id} is already on record from ${inv.reminder}.` : null,
        terms: "This records that a reminder went out. It doesn't send an email.",
        approveLabel: `Approve ${plural(n, "reminder")}`,
        describe: (results) => ({
          text: results.length === 1 ? `Done. Reminder recorded on invoice ${results[0].id}.` : `Done. Reminders recorded on ${plural(results.length, "invoice")}.`,
        }),
      };
    }

    return {
      title: `Approve ${plural(n, "change")}?`,
      head: ["What will change"],
      rows: actions.map((a) => [a.what_will_change]),
      tableOpts: {},
      headsUp: null,
      terms: n === 1 ? "" : `All ${n} changes are applied together: if one fails, none are applied.`,
      approveLabel: `Approve ${plural(n, "change")}`,
      describe: () => ({ text: "Done." }),
    };
  }

  function slip(item) {
    const actions = item.approval.actions || [item.approval];
    const cfg = slipConfig(actions, item.approval.total_cost);
    const reasons = [...new Set(actions.map((a) => a.reason))];
    const s = el("section", "slip");
    s.setAttribute("aria-label", cfg.title);
    const head = el("div", "slip-head");
    head.appendChild(el("h3", "slip-title", cfg.title));
    const state = el("span", "slip-state", "Waiting for you");
    head.appendChild(state);
    s.appendChild(head);
    s.appendChild(table(cfg.head, cfg.rows, cfg.tableOpts));
    if (cfg.headsUp) {
      const h = el("div", "heads-up");
      h.append(el("b", null, "Check before approving"), el("span", null, cfg.headsUp));
      s.appendChild(h);
    }
    if (reasons.length === 1) {
      const reason = el("p", "slip-reason");
      reason.append(el("span", null, "Reason: "), document.createTextNode(reasons[0]));
      s.appendChild(reason);
    }
    if (cfg.terms) s.appendChild(el("p", "slip-terms", cfg.terms));
    const actionsRow = el("div", "slip-actions");
    const approve = el("button", "btn btn-approve", cfg.approveLabel);
    const reject = el("button", "btn btn-reject", "Reject");
    approve.type = reject.type = "button";
    actionsRow.append(approve, reject);
    s.appendChild(actionsRow);
    const problem = el("p", "form-hint");
    problem.hidden = true;
    s.appendChild(problem);

    const entry = { title: cfg.title, node: s };
    pending.push(entry);
    renderPending();

    async function settle(approved) {
      approve.disabled = reject.disabled = true;
      problem.hidden = true;
      let res;
      try {
        res = await fetchJSON(`/api/approvals/${encodeURIComponent(item.id)}`, { method: "POST", body: JSON.stringify({ approved }) });
      } catch (e) {
        approve.disabled = reject.disabled = false;
        problem.textContent = e.message === "Something went wrong." ? "Couldn't reach the server. Try again." : e.message;
        problem.hidden = false;
        return;
      }
      pending.splice(pending.indexOf(entry), 1);
      renderPending();
      actionsRow.remove();
      const ok = approved && res.applied;
      state.textContent = `${!approved ? "Rejected" : ok ? "Approved" : "Not applied"} at ${now()}`;
      const stamp = el("div", "stamp " + (ok ? "ok" : "stop"), !approved ? "REJECTED" : ok ? "APPROVED" : "NOT APPLIED");
      stamp.setAttribute("aria-hidden", "true");
      s.appendChild(stamp);
      const out = el("div", "outcome");
      out.tabIndex = -1;
      if (approved && ok) {
        await loadRecords();
        refreshRecords();
        const result = cfg.describe(res.results);
        out.appendChild(el("p", null, result.text));
        if (result.detail) out.appendChild(result.detail);
      } else if (approved) {
        out.appendChild(el("p", "fail", "Not applied. Nothing was changed."));
      } else {
        out.appendChild(el("p", null, "Rejected. Nothing was changed."));
        out.appendChild(reviseForm(item.question));
      }
      s.appendChild(out);
      out.focus({ preventScroll: true });
    }
    approve.addEventListener("click", () => settle(true));
    reject.addEventListener("click", () => settle(false));
    return s;
  }

  function reviseForm(question) {
    const form = el("form", "revise");
    const id = "revise-" + Math.random().toString(36).slice(2, 8);
    const label = el("label", null, "What should change? I'll prepare a new slip.");
    label.htmlFor = id;
    const input = el("input");
    input.id = id;
    input.maxLength = 500;
    input.placeholder = "Describe the change";
    const row = el("div", "revise-row");
    const send = el("button", "btn btn-ask", "Prepare a new slip");
    send.type = "submit";
    const skip = el("button", "linkbtn", "No new slip needed");
    skip.type = "button";
    row.append(input, send, skip);
    form.append(label, row);
    skip.addEventListener("click", () => form.remove());
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text || busy) { input.focus(); return; }
      form.remove();
      post(text, { send: `${question} Change: ${text}` });
    });
    return form;
  }

  /* Desk thread */
  const inner = document.getElementById("thread-inner");
  const form = document.getElementById("ask-form");
  const input = document.getElementById("question");
  const askBtn = document.getElementById("ask-btn");
  const hint = document.getElementById("form-hint");
  let busy = false;

  function setBusy(b) {
    busy = b;
    input.disabled = askBtn.disabled = b;
    if (!b) input.focus({ preventScroll: true });
  }

  function exchange(text, time) {
    document.getElementById("desk-empty")?.remove();
    const ex = el("article", "exchange");
    const line = el("div", "ask-line");
    line.append(el("h2", "ask", text), el("span", "ask-time", time));
    ex.appendChild(line);
    inner.appendChild(ex);
    return ex;
  }

  function resultFor(event) {
    if (event.type === "approval") return slip(event);
    if (event.type === "answer") {
      const cut = event.truncated ? el("p", "form-hint", `Showing the first ${event.rows.length} rows. More matched; ask a narrower question to see the rest.`) : null;
      return answer({ text: event.answer, body: [resultTable(event.rows), cut], checked: event.verified });
    }
    return answer({ text: event.message || "Something went wrong. Nothing was changed." });
  }

  async function post(text, { send } = {}) {
    const ex = exchange(text, now());
    setBusy(true);
    const steps = el("ol", "steps");
    steps.setAttribute("aria-live", "polite");
    const working = el("li", "step active working");
    working.append(el("i"), el("span", null, "Working on it"));
    steps.appendChild(working);
    ex.appendChild(steps);
    ex.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });

    let result = null;
    try {
      const res = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: send || text }) });
      if (!res.ok) throw new Error(await errorText(res));
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        for (const line of lines.filter(Boolean)) {
          const event = JSON.parse(line);
          if (event.type === "step") {
            const li = el("li", "step done");
            li.append(el("i"), el("span", null, event.label));
            steps.insertBefore(li, working);
          } else {
            result = event;
          }
        }
        if (done) break;
      }
    } catch (e) {
      result = { type: "error", message: e.message === "Failed to fetch" ? "Couldn't reach the server. Nothing was changed." : e.message };
    }
    steps.remove();
    ex.appendChild(resultFor(result || { type: "error" }));
    setBusy(false);
    ex.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    const approveBtn = ex.querySelector(".btn-approve");
    if (approveBtn) approveBtn.focus({ preventScroll: true });
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (busy) return;
    const q = input.value.trim();
    if (!q) { hint.textContent = "Type a question or a change first."; hint.hidden = false; input.focus(); return; }
    hint.hidden = true;
    input.value = "";
    post(q);
  });
  input.addEventListener("input", () => { hint.hidden = true; });

  /* Records */
  function b(text) { return el("b", null, text); }
  const ENTITIES = {
    customers: {
      title: "Customers", noun: "customer",
      rows: () => DB.customers,
      search: (c) => `${c.company} ${c.name} ${c.email}`,
      filters: [{ label: "All", test: () => true }, { label: "Overdue invoices", test: (c) => overdueOf(c) > 0 }],
      columns: [
        { label: "Company", value: (c) => c.company, link: true },
        { label: "Contact", value: (c) => c.name, narrowHide: true },
        { label: "Orders", value: (c) => customerOrders(c).length, num: true },
        { label: "Spent", value: (c) => spent(c), show: (c) => fmt(spent(c)), num: true },
        { label: "Overdue", value: (c) => overdueOf(c), show: (c) => (overdueOf(c) ? fmt(overdueOf(c)) : ""), num: true, tone: "short" },
      ],
      summary: (rows) => {
        const owing = rows.filter((c) => overdueOf(c) > 0);
        const total = round2(owing.reduce((s, c) => s + overdueOf(c), 0));
        return [`${plural(rows.length, "customer")}. `, b(`${owing.length} owe ${fmt(total)}`), " in overdue invoices."];
      },
      detail: customerDetail,
    },
    products: {
      title: "Products", noun: "product",
      rows: () => DB.products,
      search: (p) => `${p.name} ${p.sku}`,
      filters: [{ label: "All", test: () => true }, { label: "Below reorder level", test: isLow }],
      columns: [
        { label: "Product", value: (p) => p.name, link: true },
        { label: "SKU", value: (p) => p.sku, narrowHide: true, nowrap: true },
        { label: "In stock", value: (p) => p.stock, num: true },
        { label: "Reorder at", value: (p) => p.thr, num: true },
        { label: "Price", value: (p) => p.price, show: (p) => fmt(p.price), num: true, narrowHide: true },
        { label: "Unit cost", value: (p) => p.cost, show: (p) => fmt(p.cost), num: true, narrowHide: true },
        { label: "Stock", value: (p) => (isLow(p) ? 0 : 1), show: (p) => statusPill(isLow(p) ? "below reorder level" : "in stock") },
      ],
      summary: (rows) => {
        const low = rows.filter(isLow).length;
        const value = round2(rows.reduce((s, p) => s + p.stock * p.cost, 0));
        return [`${plural(rows.length, "product")}, holding ${fmt(value)} of stock at cost. `, b(`${low} below their reorder level`), "."];
      },
      detail: productDetail,
    },
    orders: {
      title: "Orders", noun: "order", byNumber: true,
      rows: () => DB.orders,
      search: (o) => companyOf(o.customerId),
      filters: ["pending", "processing", "shipped", "completed", "cancelled"].reduce((f, s) => f.concat({ label: cap(s), test: (o) => o.status === s }), [{ label: "All", test: () => true }]),
      columns: [
        { label: "Order", value: (o) => o.id, show: (o) => "#" + o.id, link: true },
        { label: "Customer", value: (o) => companyOf(o.customerId) },
        { label: "Date", value: (o) => -o.daysAgo, show: (o) => dateIn(-o.daysAgo), narrowHide: true, nowrap: true },
        { label: "Items", value: (o) => o.items.length, num: true, narrowHide: true },
        { label: "Total", value: (o) => o.total, show: (o) => fmt(o.total), num: true },
        { label: "Status", value: (o) => o.status, show: (o) => statusPill(o.status) },
      ],
      summary: (rows) => {
        const live = rows.filter((o) => o.status !== "cancelled");
        const value = round2(live.reduce((s, o) => s + o.total, 0));
        const waiting = rows.filter((o) => o.status === "pending").length;
        return [`${plural(rows.length, "order")}, worth ${fmt(value)} without the cancelled ones. `, b(`${waiting} pending`), "."];
      },
      detail: orderDetail,
    },
    invoices: {
      title: "Invoices", noun: "invoice", byNumber: true,
      rows: () => DB.invoices,
      search: (i) => companyOf(i.customerId),
      filters: ["overdue", "sent", "draft", "paid"].reduce((f, s) => f.concat({ label: cap(s), test: (i) => i.status === s }), [{ label: "All", test: () => true }]),
      columns: [
        { label: "Invoice", value: (i) => i.id, show: (i) => "#" + i.id, link: true },
        { label: "Customer", value: (i) => companyOf(i.customerId) },
        { label: "Due", value: (i) => i.dueIn, show: (i) => dateIn(i.dueIn), nowrap: true, narrowHide: true },
        { label: "Amount", value: (i) => i.amount, show: (i) => fmt(i.amount), num: true },
        { label: "Status", value: (i) => i.status, show: (i) => statusPill(i.status) },
        { label: "Reminder", value: (i) => i.reminder || "", narrowHide: true, nowrap: true },
      ],
      summary: (rows) => {
        const od = rows.filter((i) => i.status === "overdue");
        const unpaid = round2(rows.filter((i) => i.status !== "paid").reduce((s, i) => s + i.amount, 0));
        return [`${plural(rows.length, "invoice")}, ${fmt(unpaid)} not yet paid. `, b(`${od.length} overdue, ${fmt(round2(od.reduce((s, i) => s + i.amount, 0)))}`), "."];
      },
      detail: invoiceDetail,
    },
    "purchase-orders": {
      title: "Purchase orders", noun: "purchase order", byNumber: true,
      rows: () => DB.purchaseOrders,
      search: (po) => (product(po.productId) || { name: "" }).name,
      filters: ["draft", "ordered", "received"].reduce((f, s) => f.concat({ label: cap(s), test: (po) => po.status === s }), [{ label: "All", test: () => true }]),
      columns: [
        { label: "Order", value: (po) => po.id, show: (po) => "#" + po.id, link: true },
        { label: "Product", value: (po) => (product(po.productId) || { name: "" }).name },
        { label: "Quantity", value: (po) => po.qty, num: true },
        { label: "Unit cost", value: (po) => po.unitCost, show: (po) => fmt(po.unitCost), num: true, narrowHide: true },
        { label: "Cost", value: (po) => po.qty * po.unitCost, show: (po) => fmt(round2(po.qty * po.unitCost)), num: true },
        { label: "Status", value: (po) => po.status, show: (po) => statusPill(po.status) },
        { label: "Created", value: (po) => -po.daysAgo, show: (po) => dateIn(-po.daysAgo), narrowHide: true, nowrap: true },
      ],
      summary: (rows) => {
        const drafts = rows.filter((po) => po.status === "draft");
        const value = round2(drafts.reduce((s, po) => s + po.qty * po.unitCost, 0));
        return [`${plural(rows.length, "purchase order")}. `, b(`${drafts.length} drafts worth ${fmt(value)}`), " haven't been sent to a supplier."];
      },
      detail: poDetail,
    },
  };

  const recState = {};
  Object.keys(ENTITIES).forEach((k) => { recState[k] = { filter: 0, q: "", sort: null, dir: 1 }; });
  let currentView = "desk";
  let openRecord = null;

  function renderRecords() {
    const key = currentView;
    const ent = ENTITIES[key];
    const st = recState[key];
    const wrap = document.getElementById("rec-table");
    document.getElementById("rec-title").textContent = ent.title;
    if (recordsError) {
      document.getElementById("rec-summary").replaceChildren();
      document.getElementById("rec-filters").replaceChildren();
      wrap.replaceChildren(el("p", "app-error", recordsError));
      return;
    }
    const all = ent.rows();
    document.getElementById("rec-summary").replaceChildren(...ent.summary(all).map((p) => (typeof p === "string" ? document.createTextNode(p) : p)));

    const filters = document.getElementById("rec-filters");
    filters.replaceChildren();
    ent.filters.forEach((f, i) => {
      const btn = el("button", "filter");
      btn.type = "button";
      btn.setAttribute("aria-pressed", String(st.filter === i));
      btn.append(f.label, el("span", "n", String(all.filter(f.test).length)));
      btn.addEventListener("click", () => { st.filter = i; renderRecords(); });
      filters.appendChild(btn);
    });
    const search = document.getElementById("rec-search");
    search.placeholder = `Search ${ent.title.toLowerCase()}`;
    if (search.value !== st.q) search.value = st.q;

    const q = st.q.trim().toLowerCase();
    const number = ent.byNumber && q.match(/^#?(\d+)$/);
    const matches = (r) => !q || (number ? r.id === +number[1] : ent.search(r).toLowerCase().includes(q));
    let rows = all.filter(ent.filters[st.filter].test).filter(matches);
    if (st.sort != null) {
      const col = ent.columns[st.sort];
      rows = [...rows].sort((a, c) => {
        const x = col.value(a), y = col.value(c);
        return (typeof x === "number" ? x - y : String(x).localeCompare(String(y))) * st.dir;
      });
    }

    wrap.replaceChildren();
    const t = el("table", "ledger");
    const cls = (c) => [c.num ? "num" : "", c.narrowHide ? "hide-narrow" : "", c.nowrap ? "nowrap" : ""].join(" ").trim();
    const hr = t.createTHead().insertRow();
    ent.columns.forEach((c, i) => {
      const th = el("th", cls(c));
      th.scope = "col";
      const sb = el("button", "sort");
      sb.type = "button";
      sb.append(c.label);
      if (st.sort === i) { th.setAttribute("aria-sort", st.dir > 0 ? "ascending" : "descending"); sb.append(el("span", null, st.dir > 0 ? "↑" : "↓")); }
      sb.addEventListener("click", () => {
        if (st.sort === i) st.dir = -st.dir; else { st.sort = i; st.dir = c.num ? -1 : 1; }
        renderRecords();
      });
      th.appendChild(sb);
      hr.appendChild(th);
    });
    const tb = t.createTBody();
    rows.forEach((r) => {
      const tr = tb.insertRow();
      if (openRecord && openRecord.key === key && openRecord.id === r.id) tr.className = "selected";
      ent.columns.forEach((c) => {
        const td = tr.insertCell();
        td.className = cls(c);
        const v = c.show ? c.show(r) : String(c.value(r));
        if (c.link) {
          const lb = el("button", "rowlink", v);
          lb.type = "button";
          lb.setAttribute("aria-label", `Open ${ent.noun} ${v}`);
          lb.addEventListener("click", (e) => { e.stopPropagation(); openDetail(key, r.id); });
          td.appendChild(lb);
        } else if (v instanceof Node) td.appendChild(v);
        else { td.textContent = v; if (c.tone && v) td.classList.add(c.tone); }
      });
      tr.addEventListener("click", () => openDetail(key, r.id));
    });
    wrap.appendChild(t);
    if (!rows.length) wrap.appendChild(el("p", "rec-empty", `No ${ent.title.toLowerCase()} match.`));
  }

  document.getElementById("rec-search").addEventListener("input", (e) => {
    recState[currentView].q = e.target.value;
    renderRecords();
  });

  function refreshRecords() {
    renderNav();
    if (ENTITIES[currentView]) renderRecords();
  }

  /* Detail panel */
  const drawer = document.getElementById("drawer");
  const scrim = document.getElementById("scrim");
  function kv(pairs) {
    const dl = el("dl", "kv");
    pairs.filter(Boolean).forEach(([k, v]) => { dl.append(el("dt", null, k)); const dd = el("dd"); if (v instanceof Node) dd.appendChild(v); else dd.textContent = v; dl.append(dd); });
    return dl;
  }
  function sectionOf(title, ...nodes) { const s = el("section"); s.append(el("h3", null, title), ...nodes); return s; }
  function recLink(key, id, label) {
    const a = el("button", "linkbtn", label);
    a.type = "button";
    a.addEventListener("click", () => openDetail(key, id));
    return a;
  }
  function openDetail(key, id) {
    const record = byId(ENTITIES[key].rows(), id);
    if (!record) return;
    openRecord = { key, id };
    const [kind, title, ...body] = ENTITIES[key].detail(record);
    drawer.replaceChildren();
    const head = el("div", "drawer-head");
    const titles = el("div");
    titles.append(el("p", "drawer-kind", kind));
    const h = el("h2", "drawer-title", title);
    h.id = "drawer-title";
    h.tabIndex = -1;
    titles.append(h);
    const close = el("button", "close", "Close");
    close.type = "button";
    close.addEventListener("click", closeDetail);
    head.append(titles, close);
    drawer.append(head, ...body.filter(Boolean));
    drawer.hidden = scrim.hidden = false;
    drawer.scrollTop = 0;
    h.focus();
    if (currentView === key) renderRecords();
  }
  function closeDetail() {
    if (drawer.hidden) return;
    drawer.hidden = scrim.hidden = true;
    openRecord = null;
    if (ENTITIES[currentView]) renderRecords();
  }
  scrim.addEventListener("click", closeDetail);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDetail(); });

  function requestChange(text) {
    closeDetail();
    go("desk");
    if (busy) { hint.textContent = "Wait for the current request to finish, then try again."; hint.hidden = false; return; }
    post(text);
  }
  function actionBox(controls, label, build) {
    const box = el("div", "action-box");
    const row = el("div", "action-row");
    controls.forEach((c) => row.appendChild(c));
    const btn = el("button", "btn btn-ask btn-sm", label);
    btn.type = "button";
    btn.addEventListener("click", () => { const text = build(); if (text) requestChange(text); });
    row.appendChild(btn);
    box.appendChild(row);
    return box;
  }

  function customerDetail(c) {
    const orders = customerOrders(c).sort((a, z) => a.daysAgo - z.daysAgo);
    const invs = customerInvoices(c).sort((a, z) => a.dueIn - z.dueIn);
    return [
      "Customer", c.company,
      kv([["Contact", c.name], ["Email", c.email], ["Customer since", dateIn(-c.since)], ["Orders", String(orders.length)], ["Spent", fmt(spent(c))], ["Overdue", overdueOf(c) ? fmt(overdueOf(c)) : "Nothing overdue"]]),
      sectionOf("Orders", orders.length ? table(["Order", "Date", "Total", "Status"], orders.map((o) => [recLink("orders", o.id, "#" + o.id), dateIn(-o.daysAgo), fmt(o.total), statusPill(o.status)]), { num: [2], nowrap: [1] }) : el("p", "empty", "No orders yet.")),
      sectionOf("Invoices", invs.length ? table(["Invoice", "Due", "Amount", "Status"], invs.map((i) => [recLink("invoices", i.id, "#" + i.id), dateIn(i.dueIn), fmt(i.amount), statusPill(i.status)]), { num: [2], nowrap: [1] }) : el("p", "empty", "No invoices yet.")),
    ];
  }

  function productDetail(p) {
    const sales = productSales().get(p.id) || { units: 0, revenue: 0 };
    const pos = DB.purchaseOrders.filter((po) => po.productId === p.id).sort((a, z) => a.daysAgo - z.daysAgo);
    const max = Math.max(p.stock, p.thr * 2, 1);
    const meter = el("div", "meter" + (isLow(p) ? " low" : ""));
    meter.setAttribute("role", "img");
    meter.setAttribute("aria-label", `${p.stock} in stock, reorder at ${p.thr}`);
    const bar = el("span");
    bar.style.width = `${(p.stock / max) * 100}%`;
    const mark = el("i");
    mark.style.left = `calc(${(p.thr / max) * 100}% - 1px)`;
    meter.append(bar, mark);
    const legend = el("div", "meter-legend");
    legend.append(el("span", null, `${p.stock} in stock`), el("span", null, `reorder at ${p.thr}`));
    const qty = el("input");
    qty.type = "number";
    qty.min = "1";
    qty.value = String(isLow(p) ? p.thr * 2 - p.stock : p.thr);
    qty.id = "po-qty";
    const qlabel = el("label", null, "Quantity");
    qlabel.htmlFor = "po-qty";
    return [
      "Product", p.name,
      sectionOf("Stock", meter, legend),
      kv([["SKU", p.sku], ["Price", fmt(p.price)], ["Unit cost", fmt(p.cost)], ["Margin", `${Math.round(((p.price - p.cost) / p.price) * 100)}%`], ["Units sold", String(sales.units)], ["Revenue", fmt(sales.revenue)]]),
      actionBox([qlabel, qty], "Prepare purchase order", () => {
        const n = parseInt(qty.value, 10);
        if (!(n > 0)) { qty.focus(); return null; }
        return `Create a purchase order for ${n} units of product ${p.id}`;
      }),
      sectionOf("Purchase orders", pos.length ? table(["Order", "Quantity", "Cost", "Status"], pos.map((po) => [recLink("purchase-orders", po.id, "#" + po.id), String(po.qty), fmt(round2(po.qty * po.unitCost)), statusPill(po.status)]), { num: [1, 2] }) : el("p", "empty", "No purchase orders yet.")),
    ];
  }

  function orderDetail(o) {
    const c = customer(o.customerId);
    const inv = invoiceForOrder(o.id);
    const lines = o.items.map((it) => [recLink("products", it.productId, (product(it.productId) || { name: "" }).name), String(it.qty), fmt(it.unitPrice), fmt(round2(it.qty * it.unitPrice))]);
    let changes = null;
    if (o.status !== "cancelled") {
      const sel = el("select");
      sel.id = "order-status";
      ["pending", "processing", "shipped", "completed"].filter((s) => s !== o.status).forEach((s) => sel.appendChild(el("option", null, cap(s))));
      const slabel = el("label", null, "Change status to");
      slabel.htmlFor = "order-status";
      changes = sectionOf("Changes", actionBox([slabel, sel], "Prepare change", () => `Change order ${o.id} status to ${sel.value.toLowerCase()}.`));
      if (o.status !== "shipped" && o.status !== "completed") changes.append(actionBox([], "Prepare cancellation", () => `Cancel order ${o.id}.`));
    }
    return [
      "Order", `Order #${o.id}`,
      kv([["Customer", c ? recLink("customers", c.id, c.company) : ""], ["Date", dateIn(-o.daysAgo)], ["Status", statusPill(o.status)], o.reason && ["Cancellation reason", o.reason], ["Invoice", inv ? frag(recLink("invoices", inv.id, "#" + inv.id), document.createTextNode(" "), statusPill(inv.status)) : "Not invoiced"]]),
      sectionOf("Items", table(["Product", "Qty", "Unit price", "Line total"], lines, { num: [1, 2, 3], foot: ["Total", "", "", fmt(o.total)] })),
      changes,
    ];
  }

  function invoiceDetail(i) {
    const c = customer(i.customerId);
    const canRemind = (i.status === "overdue" || i.status === "sent") && !i.reminder;
    return [
      "Invoice", `Invoice #${i.id}`,
      kv([["Customer", c ? recLink("customers", c.id, c.company) : ""], ["Order", recLink("orders", i.orderId, "#" + i.orderId)], ["Amount", fmt(i.amount)], ["Status", statusPill(i.status)], ["Due", dateIn(i.dueIn)], i.status === "overdue" && ["Days late", String(daysLate(i))], ["Reminder", i.reminder ? `Recorded ${i.reminder}` : "None on record"]]),
      canRemind ? actionBox([], "Prepare reminder", () => `Send a reminder for invoice ${i.id}`) : null,
    ];
  }

  function poDetail(po) {
    const p = product(po.productId);
    return [
      "Purchase order", `Purchase order #${po.id}`,
      kv([["Product", p ? recLink("products", p.id, p.name) : ""], ["Quantity", String(po.qty)], ["Unit cost", fmt(po.unitCost)], ["Cost", fmt(round2(po.qty * po.unitCost))], ["Status", statusPill(po.status)], ["Created", dateIn(-po.daysAgo)], ["Reason", po.reason]]),
    ];
  }

  /* Navigation */
  const NAV = [
    { key: "desk", label: "Desk" },
    { key: "customers", label: "Customers" },
    { key: "products", label: "Products" },
    { key: "orders", label: "Orders" },
    { key: "invoices", label: "Invoices" },
    { key: "purchase-orders", label: "Purchase orders" },
  ];
  function renderNav() {
    const nav = document.getElementById("nav");
    const strip = document.getElementById("nav-strip");
    nav.replaceChildren();
    strip.replaceChildren();
    NAV.forEach((n) => {
      const a = el("a");
      a.href = "#" + n.key;
      a.append(el("span", null, n.label));
      if (ENTITIES[n.key] && !recordsError) a.append(el("span", "count", String(ENTITIES[n.key].rows().length)));
      const s = el("a", null, n.label);
      s.href = "#" + n.key;
      [a, s].forEach((x) => { if (n.key === currentView) x.setAttribute("aria-current", "page"); });
      nav.appendChild(a);
      strip.appendChild(s);
    });
  }
  function show(view) {
    currentView = ENTITIES[view] ? view : "desk";
    closeDetail();
    document.getElementById("view-desk").hidden = currentView !== "desk";
    document.getElementById("view-records").hidden = currentView === "desk";
    renderNav();
    if (currentView !== "desk") { renderRecords(); document.querySelector(".rec-scroll").scrollTop = 0; }
  }
  function go(view) {
    if (location.hash.slice(1) !== view) location.hash = view;
    show(view);
  }
  window.addEventListener("hashchange", () => show(location.hash.slice(1)));

  /* Access key */
  function showLogin() {
    document.getElementById("view-desk").hidden = true;
    document.getElementById("view-records").hidden = true;
    document.getElementById("view-login").hidden = false;
    const form = document.getElementById("login-form");
    const input = document.getElementById("access-key");
    const hint = document.getElementById("login-hint");
    input.focus();
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!input.value) return;
      try {
        await fetchJSON("/api/session", { method: "POST", body: JSON.stringify({ key: input.value }) });
        location.reload();
      } catch (e) {
        hint.textContent = e.message;
        hint.hidden = false;
        input.select();
      }
    });
  }

  /* Start */
  (async function start() {
    const session = await fetch("/api/session").catch(() => null);
    if (session && session.status === 401) return showLogin();
    renderPending();
    show(location.hash.slice(1));
    await loadRecords();
    refreshRecords();
    try {
      const waiting = await fetchJSON("/api/approvals");
      waiting.forEach((item) => exchange(item.question, "").appendChild(slip(item)));
    } catch (e) { /* the Desk still works; waiting changes reappear after a reload */ }
  })();
})();
