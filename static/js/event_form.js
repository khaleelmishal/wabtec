/* Step-by-step event form with background saving.
   Needs: app.js (WPU.init, WPU.toast, WPU.confirm, WPU.csrf) and the markup in events/event_form.html. */
(function () {
  "use strict";
  var root = document.getElementById("evform");
  var cfgEl = document.getElementById("ev-config");
  if (!root || !cfgEl) return;

  var cfg = JSON.parse(cfgEl.textContent);
  var steps = cfg.steps, first = steps[0], last = steps[steps.length - 1];
  var eventId = cfg.id, current = null;
  var dirty = {}, timers = {}, chain = Promise.resolve(), retryTimer = null, problem = false;

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var panel = function (n) { return $('form.step-panel[data-step="' + n + '"]'); };
  var esc = function (t) { var d = document.createElement("div"); d.textContent = t; return d.innerHTML; };

  /* ---------------------------------------------------------- save indicator */
  var stateEl = $("#saveState");
  function setState(kind, text) {
    var icons = { saving: "bi-arrow-repeat", saved: "bi-cloud-check", error: "bi-exclamation-triangle", offline: "bi-wifi-off" };
    stateEl.className = "save-state " + kind;
    stateEl.innerHTML = "";
    var i = document.createElement("i"); i.className = "bi " + icons[kind];
    var s = document.createElement("span"); s.textContent = " " + text;
    stateEl.appendChild(i); stateEl.appendChild(s);
    problem = kind === "error" || kind === "offline";
  }

  /* ---------------------------------------------------------- helpers */
  function saveUrl(n) {
    return eventId ? cfg.urls.save.replace("{id}", eventId).replace("{step}", n)
                   : cfg.urls.save_new.replace("{step}", n);
  }
  function live(name, val) {
    if (val == null) return;
    $$('[data-live="' + name + '"]').forEach(function (el) { el.textContent = val; });
  }
  function clearErrors(n) {
    var f = panel(n); if (!f) return;
    $$("[data-err]", f).forEach(function (e) { e.textContent = ""; });
    $$(".is-invalid", f).forEach(function (e) { e.classList.remove("is-invalid"); });
  }
  function showErrors(n, errs) {
    var f = panel(n); if (!f) return;
    Object.keys(errs).forEach(function (k) {
      var box = $('[data-err="' + k + '"]', f);
      if (box) box.textContent = errs[k].join(" ");
      var el = f.elements[k];
      if (el && el.classList) el.classList.add("is-invalid");
    });
  }

  /* ---------------------------------------------------------- sidebar */
  var activeFilter = "all";
  function applyFilter() {
    $$("#reportList li[data-status]").forEach(function (li) {
      li.classList.toggle("d-none", activeFilter !== "all" && li.dataset.status !== activeFilter);
    });
  }
  function refreshCounts() {
    var lis = $$("#reportList li[data-status]");
    var c = { all: lis.length, ongoing: 0, finished: 0 };
    lis.forEach(function (li) { c[li.dataset.status]++; });
    Object.keys(c).forEach(function (k) {
      var el = $('[data-count="' + k + '"]'); if (el) el.textContent = c[k];
    });
  }
  function updateSidebar(d) {
    var list = $("#reportList"); if (!list || !d.item_html) return;
    var tmp = document.createElement("ul"); tmp.innerHTML = d.item_html.trim();
    var li = tmp.firstElementChild;
    var old = list.querySelector('[data-event-id="' + d.id + '"]');
    if (old) old.remove();
    var none = $("#noReports"); if (none) none.remove();
    $$("#reportList li.active").forEach(function (x) { x.classList.remove("active"); });
    list.insertBefore(li, list.firstChild);
    applyFilter(); refreshCounts();
  }
  $$("[data-filter]").forEach(function (b) {
    b.addEventListener("click", function () {
      activeFilter = b.dataset.filter;
      $$("[data-filter]").forEach(function (x) { x.classList.toggle("active", x === b); });
      applyFilter();
    });
  });

  /* ---------------------------------------------------------- saving */
  function handleSave(n, d) {
    clearErrors(n);
    if (!d.ok) {
      showErrors(n, d.errors || {});
      dirty[n] = true;
      setState("error", "Not saved - check the highlighted fields");
      return "invalid";
    }
    if (d.id && !eventId) {
      eventId = d.id;
      history.replaceState(null, "", cfg.urls.edit.replace("{id}", d.id) + "#step-" + (current || n));
    }
    if (d.id) {
      live("event_no", d.event_no); live("issue_ref", d.issue_ref); live("reported_at", d.reported_at);
      var b = $("#statusBadge");
      if (b) { b.textContent = d.finished ? "Finished" : "Ongoing"; b.className = "badge ms-2 " + (d.finished ? "text-bg-success" : "text-bg-warning"); }
      updateSidebar(d);
    }
    if (d.followup_ids) applyFollowupIds(d.followup_ids);
    setState("saved", "Saved " + d.saved_at);
    return "ok";
  }

  function doSave(n, force) {
    if (!dirty[n] && !(force && eventId)) return Promise.resolve("ok");
    clearTimeout(timers[n]);
    var f = panel(n); if (!f) return Promise.resolve("ok");
    var fd = new FormData(f);
    fd.append("current_step", String(current || n));
    dirty[n] = false;
    setState("saving", "Saving...");
    return fetch(saveUrl(n), {
      method: "POST", body: fd, credentials: "same-origin", keepalive: true,
      headers: { "X-CSRFToken": WPU.csrf, "X-Requested-With": "XMLHttpRequest" }
    }).then(function (r) {
      var ct = r.headers.get("content-type") || "";
      if (r.redirected || ct.indexOf("json") < 0) throw { auth: true };
      return r.json();
    }).then(function (d) {
      return handleSave(n, d);
    }).catch(function (err) {
      dirty[n] = true;
      if (err && err.auth) {
        setState("error", "Session expired - log in again");
        WPU.toast("error", "Your session expired. Log in again, then reopen this report.");
        return "invalid";
      }
      setState("offline", "Offline - will retry");
      scheduleRetry();
      return "offline";
    });
  }

  /* all saves run one after another, so a new event is only ever created once */
  function enqueue(n, force) {
    var p = chain.then(function () { return doSave(n, force); });
    chain = p.catch(function () { return "offline"; });
    return p;
  }
  function queue(n, ms) {
    clearTimeout(timers[n]);
    timers[n] = setTimeout(function () { enqueue(n); }, ms);
  }
  function flushAll() {
    var result = "ok";
    var run = Promise.resolve();
    steps.forEach(function (n) {
      run = run.then(function () { return enqueue(n); }).then(function (r) {
        if (r === "invalid") result = "invalid";
        else if (r === "offline" && result === "ok") result = "offline";
      });
    });
    return run.then(function () { return result; });
  }
  function scheduleRetry() {
    if (retryTimer) return;
    retryTimer = setTimeout(function () { retryTimer = null; flushAll(); }, 10000);
  }
  window.addEventListener("online", flushAll);
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") flushAll(); });
  window.addEventListener("pagehide", flushAll);
  window.addEventListener("beforeunload", function (e) {
    if (problem) { e.preventDefault(); e.returnValue = ""; }
  });

  /* ---------------------------------------------------------- dirty tracking, conditional fields */
  function evalShow(form) {
    $$("[data-show-when]", form).forEach(function (el) {
      var p = el.dataset.showWhen.split("="), ctl = form.elements[p[0]];
      el.classList.toggle("d-none", p[1].split("|").indexOf(ctl ? ctl.value : "") === -1);
    });
  }
  $$("form.step-panel").forEach(function (f) {
    var n = +f.dataset.step;
    f.addEventListener("submit", function (e) { e.preventDefault(); });
    ["input", "change"].forEach(function (type) {
      f.addEventListener(type, function (e) {
        var t = e.target;
        if (!t.name) return;                      // flatpickr's visible copy has no name
        dirty[n] = true;
        var typing = type === "input" && (t.tagName === "TEXTAREA" || t.type === "text" || t.type === "number");
        queue(n, typing ? 1500 : 400);
        if (type === "change") evalShow(f);
      });
    });
    evalShow(f);
  });

  /* delay hours: worked out from the two times until the person types their own */
  (function () {
    var f = panel(4); if (!f) return;
    var a = f.elements.delay_from, b = f.elements.delay_to, h = f.elements.hours_lost;
    if (!a || !b || !h) return;
    h.addEventListener("input", function () { h.dataset.auto = ""; });
    function calc() {
      if (!a.value || !b.value) return;
      if (h.value && h.dataset.auto !== "1") return;
      var m = (+b.value.slice(0, 2)) * 60 + (+b.value.slice(3, 5)) - ((+a.value.slice(0, 2)) * 60 + (+a.value.slice(3, 5)));
      if (m < 0) m += 1440;
      h.value = (m / 60).toFixed(2); h.dataset.auto = "1";
      h.dispatchEvent(new Event("change", { bubbles: true }));
    }
    a.addEventListener("change", calc); b.addEventListener("change", calc);
  })();

  /* ---------------------------------------------------------- follow-up actions (step 7) */
  var fuRows = $("#fuRows"), fuTpl = $("#fuTemplate");
  function fuRowList() { return fuRows ? $$(".fu-row", fuRows) : []; }
  if (fuRows && fuTpl) {
    $("#fuAdd").addEventListener("click", function () {
      var node = fuTpl.content.firstElementChild.cloneNode(true);
      fuRows.appendChild(node);
      WPU.init(node);
      var ta = $(".fu-action", node); if (ta) ta.focus();
    });
    fuRows.addEventListener("click", function (e) {
      var btn = e.target.closest(".fu-del"); if (!btn) return;
      var row = btn.closest(".fu-row"), id = $('[name="fu_id"]', row).value;
      if (id) { $('[name="fu_delete"]', row).value = "1"; row.classList.add("d-none"); }
      else row.remove();
      dirty[7] = true; queue(7, 300);
    });
  }
  function applyFollowupIds(ids) {
    var rows = fuRowList();
    ids.forEach(function (id, i) {
      var row = rows[i]; if (!row) return;
      if (id) { $('[name="fu_id"]', row).value = id; }
      else if ($('[name="fu_delete"]', row).value === "1") { row.remove(); }
    });
  }

  /* ---------------------------------------------------------- steps */
  function show(n) {
    current = n;
    $$("form.step-panel").forEach(function (f) { f.classList.toggle("d-none", +f.dataset.step !== n); });
    $$("#stepper li").forEach(function (li) {
      var k = +li.dataset.step;
      li.classList.toggle("active", k === n); li.classList.toggle("done", k < n);
    });
    var idx = steps.indexOf(n);
    $("#stepLabel").textContent = "Step " + (idx + 1) + " of " + steps.length;
    $("#btnBack").disabled = idx === 0;
    $("#btnNext").classList.toggle("d-none", n === last);
    var sb = $("#btnSubmit"); if (sb) sb.classList.toggle("d-none", n !== last);
    history.replaceState(null, "", location.pathname + location.search + "#step-" + n);
    var top = $("#evTop"); if (top && top.scrollIntoView) top.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  function go(n) {
    if (n == null || n === current) return;
    var from = current;
    enqueue(from).then(function (r) {
      if (r === "invalid") { WPU.toast("warning", "Fix the highlighted fields first."); return; }
      show(n);
      if (eventId) enqueue(n, true);           // remember which step the person is on
    });
  }
  $("#btnNext").addEventListener("click", function () { go(steps[steps.indexOf(current) + 1]); });
  $("#btnBack").addEventListener("click", function () { go(steps[steps.indexOf(current) - 1]); });
  $$("#stepper button").forEach(function (b) { b.addEventListener("click", function () { go(+b.dataset.goto); }); });

  /* ---------------------------------------------------------- submit */
  var sub = $("#btnSubmit");
  if (sub) sub.addEventListener("click", function () {
    flushAll().then(function (r) {
      if (r === "invalid") { WPU.toast("warning", "Fix the highlighted fields first."); return; }
      if (r === "offline") { WPU.toast("warning", "You are offline. Try again when you have signal."); return; }
      if (!eventId) { WPU.toast("info", "Nothing to submit yet. Fill in the form first."); return; }
      var ask = window.EvReview ? window.EvReview.confirm()
        : WPU.confirm("It will move to Finished. You can still reopen it, and every change is logged.",
            { title: "Submit this event?", ok: "Submit" });
      return ask.then(function (ok) {
        if (!ok) return;
        return fetch(cfg.urls.submit.replace("{id}", eventId), {
          method: "POST", credentials: "same-origin",
          headers: { "X-CSRFToken": WPU.csrf, "X-Requested-With": "XMLHttpRequest" }
        }).then(function (resp) { return resp.json(); }).then(function (d) {
          if (d.ok) { window.location = d.redirect; return; }
          if (!d.errors || !Object.keys(d.errors).length) {      // 404 / 500 / plain-message errors
            Swal.fire({ icon: "error", title: "Not submitted", confirmButtonColor: "#C8102E",
              text: d.error || (d.summary || []).join(" ") || "Could not submit. Try again." });
            return;
          }
          var firstStep = null;
          Object.keys(d.errors).forEach(function (s) {
            clearErrors(+s); showErrors(+s, d.errors[s]);
            if (firstStep === null) firstStep = +s;
          });
          if (firstStep !== null) show(firstStep);
          Swal.fire({
            icon: "error", title: "Not ready to submit", confirmButtonColor: "#C8102E",
            html: "<ul style='text-align:left'>" + (d.summary || []).map(function (t) { return "<li>" + esc(t) + "</li>"; }).join("") + "</ul>"
          });
        }).catch(function () {
          WPU.toast("error", "Could not reach the server or your session expired. Your answers are saved - try again.");
        });
      });
    });
  });

  /* ---------------------------------------------------------- start */
  var m = /^#step-(\d+)/.exec(location.hash);
  var start = m && steps.indexOf(+m[1]) > -1 ? +m[1] : (steps.indexOf(cfg.start) > -1 ? cfg.start : first);
  show(start);
  refreshCounts();
})();