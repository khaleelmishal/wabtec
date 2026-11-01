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
  var locked = cfg.locked_steps || [];          // steps that cannot be changed (a submitted report: all of them)
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
    if (cfg.review && n === 7 && eventId) return cfg.urls.review_save.replace("{id}", eventId);
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
  // the tabs, counts and delete button are in _sidebar.html (window.RepList); this only puts the saved report on top
  function updateSidebar(d) {
    var list = $("#reportList"); if (!list || !d.item_html) return;
    var tmp = document.createElement("ul"); tmp.innerHTML = d.item_html.trim();
    var li = tmp.firstElementChild;
    var old = list.querySelector('[data-event-id="' + d.id + '"]');
    if (old) old.remove();
    var none = $("#noReports"); if (none) none.remove();
    $$("#reportList li.active").forEach(function (x) { x.classList.remove("active"); });
    list.insertBefore(li, list.firstChild);
    if (window.RepList) window.RepList.refresh();
  }

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
      if (b) { b.textContent = d.finished ? "Submitted" : "Draft"; b.className = "badge ms-2 " + (d.finished ? "text-bg-primary" : "text-bg-warning"); }
      updateSidebar(d);
    }
    if (d.followup_ids) applyFollowupIds(d.followup_ids);
    if (d.problems) setProblems(d.problems);
    setState("saved", "Saved " + d.saved_at);
    return "ok";
  }

  function doSave(n, force) {
    if (locked.indexOf(n) > -1) return Promise.resolve("ok");     // read only: nothing to save
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
    if (problem || (window.EvUploads && window.EvUploads.busy())) { e.preventDefault(); e.returnValue = ""; }
  });

/* ======================================================================
   KM Marker input restriction: allow only numbers and single decimal dot
   ====================================================================== */
(function () {
  var f = panel(1); if (!f) return;
  var kmInput = f.elements["km_marker"];
  if (!kmInput) return;

  kmInput.addEventListener("input", function () {
    var val = kmInput.value;
    var cleaned = val.replace(/[^0-9.]/g, "");
    var parts = cleaned.split(".");
    if (parts.length > 2) {
      cleaned = parts[0] + "." + parts.slice(1).join("");
    }
    if (val !== cleaned) {
      kmInput.value = cleaned;
    }
  });

  kmInput.addEventListener("keydown", function (e) {
    var allowedKeys = ["Backspace", "Delete", "Tab", "Escape", "Enter", ".", "Decimal"];
    if (
      allowedKeys.indexOf(e.key) !== -1 ||
      (e.key >= "0" && e.key <= "9") ||
      ((e.ctrlKey || e.metaKey) && ["a", "c", "v", "x"].indexOf(e.key.toLowerCase()) !== -1) ||
      e.key.indexOf("Arrow") === 0
    ) {
      if (e.key === "." && kmInput.value.indexOf(".") !== -1) {
        var start = kmInput.selectionStart;
        var end = kmInput.selectionEnd;
        var selectedText = kmInput.value.substring(start, end);
        if (selectedText.indexOf(".") === -1) {
          e.preventDefault();
        }
      }
      return;
    }
    e.preventDefault();
  });
})();

/* ---------------------------------------------------------- Ongoing event: toggle time_ended state */
  (function () {
    var f = panel(1); if (!f) return;
    var crewEl = f.elements["crew_involved"];
    if (crewEl && crewEl.tomselect) {
      crewEl.tomselect.settings.create = true;
      crewEl.tomselect.settings.createFilter = function(input) { return input.length > 1; };
    }
    var discEl = f.elements["discipline"];
    if (discEl && discEl.tomselect) {
      discEl.tomselect.settings.create = true;
    }

    /* clearer "add new" wording in the two pickers that allow typing a new value */
    function createLabel(ts, make) {
      if (!ts) return;
      ts.settings.render = ts.settings.render || {};
      ts.settings.render.option_create = function (data, escape) { return '<div class="create">' + make(escape(data.input)) + "</div>"; };
    }
    createLabel(crewEl && crewEl.tomselect, function (t) { return "Add <strong>" + t + "</strong> as an external person"; });
    createLabel(discEl && discEl.tomselect, function (t) { return "Add discipline <strong>" + t + "</strong>"; });

    /* quick chips under the date / time pickers: [label, days back] */
    function addChips(name, items) {
      var el = f.elements[name]; if (!el || !el._flatpickr) return;
      var fp = el._flatpickr, box = document.createElement("div");
      box.className = "quick-chips";
      items.forEach(function (it) {
        var b = document.createElement("button");
        b.type = "button"; b.textContent = it[0];
        b.addEventListener("click", function () { fp.setDate(new Date(Date.now() - it[1] * 864e5), true); });
        box.appendChild(b);
      });
      (fp.altInput || el).insertAdjacentElement("afterend", box);
    }
    addChips("event_date", [["Today", 0], ["Yesterday", 1]]);
    addChips("time_started", [["Now", 0]]);
    addChips("time_ended", [["Now", 0]]);
    var ongoingEl = f.elements["is_ongoing"];
    var timeEndedEl = f.elements["time_ended"];
    if (!ongoingEl || !timeEndedEl) return;

    function updateTimeEndedState() {
      var isOngoing = ongoingEl.checked;

      // UI cues: pulsing dot on the switch, "Still running" placeholder, chips off
      var cell = ongoingEl.closest(".fcell"); if (cell) cell.classList.toggle("is-live", isOngoing);
      if (timeEndedEl._flatpickr && timeEndedEl._flatpickr.altInput) timeEndedEl._flatpickr.altInput.placeholder = isOngoing ? "Still running" : "";
      $$(".quick-chips button", timeEndedEl.closest(".val")).forEach(function (b) { b.disabled = isOngoing; });

      // 1. Disable or enable the native input
      timeEndedEl.disabled = isOngoing;

      // 2. Handle Flatpickr / time picker plugin if present
      if (timeEndedEl._flatpickr) {
        timeEndedEl._flatpickr.set("clickOpens", !isOngoing);
        if (timeEndedEl._flatpickr.altInput) {
          timeEndedEl._flatpickr.altInput.disabled = isOngoing;
          timeEndedEl._flatpickr.altInput.classList.toggle("text-muted", isOngoing);
          timeEndedEl._flatpickr.altInput.classList.toggle("bg-light", isOngoing);
        }
      }

      // 3. Apply visual styling to the input or its wrapper container
      var wrapper = timeEndedEl.closest(".input-group") || timeEndedEl.parentElement;
      if (wrapper) {
        wrapper.classList.toggle("opacity-50", isOngoing);
      }

      if (isOngoing) {
        timeEndedEl.value = "";
        if (timeEndedEl._flatpickr) {
          timeEndedEl._flatpickr.clear();
        }
        timeEndedEl.classList.add("text-muted", "bg-light");
        // Trigger change so dirty tracking registers the cleared value
        timeEndedEl.dispatchEvent(new Event("change", { bubbles: true }));
      } else {
        timeEndedEl.classList.remove("text-muted", "bg-light");
      }
    }

    ongoingEl.addEventListener("change", updateTimeEndedState);
    updateTimeEndedState(); // Run on load in case of pre-filled data
  })();

/* ---------------------------------------------------------- Duration readout (step 1) */
  (function () {
    var f = panel(1); if (!f) return;
    var out = $("#durOut"), d = f.elements.event_date, a = f.elements.time_started,
        b = f.elements.time_ended, o = f.elements.is_ongoing;
    if (!out || !a || !b || !o) return;
    function mins(t) { return t && t.length >= 5 ? (+t.slice(0, 2)) * 60 + (+t.slice(3, 5)) : null; }
    function fmt(m) { var h = Math.floor(m / 60), r = m % 60; return h ? h + " h" + (r ? " " + r + " min" : "") : r + " min"; }
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    function render() {
      var s = mins(a.value), e = mins(b.value), txt, warn = false, now = new Date();
      var today = now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate());
      if (s === null) {
        txt = "Enter the start time";
      } else if (o.checked) {
        if (!d || !d.value || d.value === today) {
          var m = now.getHours() * 60 + now.getMinutes() - s;
          if (m < 0) { txt = "Start time is in the future"; warn = true; }
          else txt = "Running for " + fmt(m) + " so far";
        } else {
          var shown = d._flatpickr && d._flatpickr.altInput ? d._flatpickr.altInput.value : d.value;
          txt = "Ongoing since " + a.value + " on " + shown;
        }
      } else if (e === null) {
        txt = "Enter the end time, or switch on Still happening";
      } else {
        var diff = e - s, over = diff < 0;
        if (over) diff += 1440;
        txt = fmt(diff) + (over ? " (crosses midnight)" : "");
        warn = over || diff > 16 * 60;
      }
      out.textContent = txt;
      out.classList.toggle("warn", warn);
    }
    ["change", "input"].forEach(function (ev) { f.addEventListener(ev, render); });
    render();
    setInterval(render, 60000);   // keeps "Running for ..." fresh
  })();

/* ======================================================================
   Step 4: plant delay list. One searchable picker adds rows (no modal),
   rows hold the hours, "Same hours for all" and the total keep them tidy.
   Field names are the Django formset's, so saving works as before.
   ====================================================================== */
(function () {
  "use strict";

  var list = document.getElementById("plantTableBody");
  var form4 = document.querySelector('form.step-panel[data-step="4"]');
  if (!list || !form4) return;
  var totalEl = form4.querySelector('[name$="-TOTAL_FORMS"]');
  if (!totalEl) return;
  var prefix = totalEl.name.replace("-TOTAL_FORMS", "");
  var noMsg = document.getElementById("noPlantsMsg");
  var totalOut = document.getElementById("plantTotal");
  var same = document.getElementById("chkSameDelay");
  var sel = document.getElementById("plantPicker");

  /* ---- the plant catalogue, read from the picker's <option> tags ---- */
  var catalog = {};
  if (sel) {
    Array.prototype.forEach.call(sel.options, function (o) {
      if (!o.value) return;
      var p = { value: o.value, asset: o.getAttribute("data-asset") || o.textContent, model: o.getAttribute("data-model") || "",
                reg: o.getAttribute("data-reg") || "", who: o.getAttribute("data-who") || "" };
      p.text = [p.asset, p.model, p.reg, p.who].filter(Boolean).join(" ");
      catalog[o.value] = p;
    });
  }

  /* ---- helpers ---- */
  function rows() { return Array.prototype.slice.call(list.querySelectorAll("li.plant-delay-row")); }
  function isLive(r) { return !r.classList.contains("d-none"); }
  function plantIdOf(r) { var i = r.querySelector(".plant-id"); return i ? i.value : ""; }
  function hoursOf(r) { return r.querySelector(".plant-hours"); }
  function save() { form4.dispatchEvent(new Event("change", { bubbles: true })); }   // autosave

  function refresh() {
    var live = rows().filter(isLive);
    if (noMsg) noMsg.classList.toggle("d-none", live.length > 0);
    if (!totalOut) return;
    var sum = 0, any = false;
    live.forEach(function (r) {
      var v = parseFloat(hoursOf(r).value);
      if (!isNaN(v)) { sum += v; any = true; }
    });
    totalOut.textContent = live.length
      ? live.length + (live.length === 1 ? " plant" : " plants") + (any ? " - " + (Math.round(sum * 100) / 100) + " h lost in total" : "")
      : "";
    totalOut.classList.toggle("d-none", !live.length);
  }

  function liveHours() { return rows().filter(isLive).map(hoursOf); }
  function setAll(value, source) {
    liveHours().forEach(function (i) { if (i !== source && i.value !== value) i.value = value; });
  }
  function syncSame() {
    if (!same || !same.checked) return;
    var first = liveHours().filter(function (i) { return i.value.trim() !== ""; })[0];
    if (first) setAll(first.value, first);
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }
  function hidden(name, value) {
    var i = document.createElement("input");
    i.type = "hidden"; i.name = name; i.value = value;
    return i;
  }

  function buildRow(idx, p) {
    var li = el("li", "plant-delay-row");
    var base = prefix + "-" + idx + "-";
    li.appendChild(hidden(base + "id", ""));
    var pid = hidden(base + "plant", p.value);
    pid.className = "plant-id";
    li.appendChild(pid);

    var name = el("div", "pl-name");
    name.appendChild(el("strong", "", p.asset));
    name.appendChild(document.createTextNode(" "));
    name.appendChild(el("span", "", p.model + (p.reg ? " (" + p.reg + ")" : "")));
    li.appendChild(name);

    var hrs = el("div", "pl-hours");
    var h = document.createElement("input");
    h.type = "number"; h.name = base + "hours_lost"; h.className = "form-control plant-hours";
    h.step = "1"; h.min = "0"; h.placeholder = "Hours"; h.setAttribute("inputmode", "numeric");
    hrs.appendChild(h);
    hrs.appendChild(el("span", "unit", "h"));
    li.appendChild(hrs);

    // DELETE keeps the formset indexes intact when a new row is removed before saving
    var holder = el("span", "d-none");
    var del = document.createElement("input");
    del.type = "checkbox"; del.name = base + "DELETE"; del.value = "on";
    holder.appendChild(del);
    li.appendChild(holder);

    var rm = el("button", "btn-remove-plant");
    rm.type = "button"; rm.title = "Remove plant"; rm.setAttribute("aria-label", "Remove plant");
    var ic = el("i", "bi bi-x-lg"); ic.setAttribute("aria-hidden", "true");
    rm.appendChild(ic);
    li.appendChild(rm);
    return li;
  }

  /* ---- add / remove ---- */
  var picker = null;

  function addPlant(id) {
    var p = catalog[id];
    if (!p) return;
    var back = rows().filter(function (r) { return !isLive(r) && plantIdOf(r) === id; })[0];
    if (back) {                                        // it was removed earlier: bring that row back
      back.classList.remove("d-none");
      var d = back.querySelector('input[name$="-DELETE"]');
      if (d) d.checked = false;
    } else {
      var idx = parseInt(totalEl.value, 10) || 0;
      list.appendChild(buildRow(idx, p));
      totalEl.value = String(idx + 1);
    }
    syncSame();
    refresh();
    save();
  }

  list.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest(".btn-remove-plant") : null;
    if (!btn) return;
    var row = btn.closest("li.plant-delay-row");
    if (!row) return;
    var del = row.querySelector('input[name$="-DELETE"]');
    if (del) { del.checked = true; row.classList.add("d-none"); } else { row.remove(); }
    var id = plantIdOf(row);                           // offer it in the picker again
    if (picker && catalog[id] && !picker.options[id]) picker.addOption(catalog[id]);
    syncSame();
    refresh();
    save();
  });

  list.addEventListener("input", function (e) {
    if (!e.target.classList.contains("plant-hours")) return;
    if (same && same.checked) setAll(e.target.value, e.target);
    refresh();
  });

  if (same) same.addEventListener("change", function () {
    if (!same.checked) return;
    syncSame();
    refresh();
    save();
  });

  /* ---- the searchable picker ---- */
  var inUse = {};
  rows().filter(isLive).forEach(function (r) { inUse[plantIdOf(r)] = true; });

  if (sel && window.TomSelect) {
    var opts = Object.keys(catalog).filter(function (k) { return !inUse[k]; }).map(function (k) { return catalog[k]; });
    sel.innerHTML = "";                                // options now come from the list above
    picker = new TomSelect(sel, {
      options: opts,
      valueField: "value", labelField: "text", searchField: ["text"],
      maxItems: null, hideSelected: false, closeAfterSelect: false, openOnFocus: true, maxOptions: null,
      dropdownParent: "body",                       // floats over the page instead of opening inside the list
      placeholder: sel.getAttribute("placeholder") || "Search plant...",
      render: {
        option: function (d, esc) {
          return '<div class="pk-opt"><strong>' + esc(d.asset) + '</strong><span>' + esc(d.model) +
                 (d.reg ? " (" + esc(d.reg) + ")" : "") + (d.who ? " - " + esc(d.who) : "") + "</span></div>";
        },
        no_results: function () { return '<div class="no-results">No plant matches that search</div>'; }
      },
      onItemAdd: function (value) {
        addPlant(value);
        this.removeItem(value, true);                  // the picker stays empty, ready for the next plant
        this.removeOption(value, true);                // and it no longer offers a plant already in the list
        this.setTextboxValue("");
        this.refreshOptions(false);
        
      }
    });
  } else if (sel) {                                    // no Tom Select: plain multi-select fallback
    Array.prototype.forEach.call(sel.options, function (o) { if (inUse[o.value]) o.remove(); });
    sel.addEventListener("change", function () {
      Array.prototype.slice.call(sel.selectedOptions).forEach(function (o) { addPlant(o.value); o.remove(); });
    });
  }

  refresh();
})();

/* ======================================================================
   Step 7: automatic cost estimate. Same idea as the plant list: one searchable
   picker adds a row, each row holds the quantity, and the price updates on every
   add, remove or change. The server works out the same sums when it saves.
   Labour and plant: quantity x rate (Working or Standby).
   Materials and preliminaries: unit cost x quantity x (1 + markup).
   ====================================================================== */
(function () {
  "use strict";
  var form7 = document.querySelector('form.step-panel[data-step="7"]');
  var dataEl = document.getElementById("ev-cost");
  var list = document.getElementById("costList");
  if (!form7 || !dataEl || !list) return;

  var data = JSON.parse(dataEl.textContent);
  var markup = parseFloat(data.settings.markup) || 0, gstPct = parseFloat(data.settings.gst) || 0;
  var lump = parseFloat(data.settings.lump_sum) || 0;
  var sel = document.getElementById("costPicker"), noMsg = document.getElementById("noCostMsg");
  var chkNo = document.getElementById("chkNoCost"), chkGst = document.getElementById("chkGst");
  var pickerWrap = document.getElementById("costPickerWrap"), body = document.getElementById("costBody");
  var catalog = {};
  data.catalog.forEach(function (i) { catalog[i.id] = i; });
  var picker = null;

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }
  function hidden(name, value) { var i = document.createElement("input"); i.type = "hidden"; i.name = name; i.value = value; return i; }
  function save() { form7.dispatchEvent(new Event("change", { bubbles: true })); }          // autosave
  function round2(x) { return Math.round((x + Number.EPSILON) * 100) / 100; }
  function fmt(x) { return "$" + round2(x).toLocaleString("en-AU", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function num(i) { var v = parseFloat(i.value); return isNaN(v) ? 0 : v; }
  function rows() { return Array.prototype.slice.call(list.querySelectorAll("li.cost-row")); }
  function put(id, text) { var e = document.getElementById(id); if (e) e.textContent = text; }
  function unitWord(it) { return /^day/i.test(it.uom) ? "days" : (/^hr/i.test(it.uom) ? "h" : it.uom.toLowerCase()); }

  var chkSameQty = document.getElementById("chkSameQty");

  function liveQuantities() {
    return Array.prototype.slice.call(list.querySelectorAll(".cost-qty"));
  }

  function setAllQuantities(value, source) {
    liveQuantities().forEach(function (input) {
      if (input !== source && input.value !== value) {
        input.value = value;
        input.dispatchEvent(new Event("input", { bubbles: true })); // Triggers recalc & autosave
      }
    });
  }

  function syncSameQuantities() {
    if (!chkSameQty || !chkSameQty.checked) return;
    var inputs = liveQuantities();
    var first = inputs.filter(function (input) { return input.value.trim() !== ""; })[0];
    if (first) {
      setAllQuantities(first.value, first);
    }
  }

  function rateText(it, type) {
    if (it.cat === "material") return it.no + " \u00b7 cost + " + markup + "%";
    var r = parseFloat(type === "standby" ? it.s : it.w) || 0;
    return it.no + " \u00b7 " + fmt(r) + " / " + it.uom + " (" + (type === "standby" ? "standby" : "working") + ")";
  }

  function lineTotal(row) {
    var it = catalog[row.dataset.id]; if (!it) return 0;
    var qty = num(row.querySelector(".cost-qty"));
    if (it.cat === "material") return num(row.querySelector(".cost-unit-in")) * qty * (1 + markup / 100);
    var type = row.querySelector('input[name="cost_type"]').value;
    return (parseFloat(type === "standby" ? it.s : it.w) || 0) * qty;
  }

  function recalc() {
    var sums = { labour: 0, plant: 0, material: 0 };
    rows().forEach(function (r) {
      var t = lineTotal(r);
      r.querySelector(".cr-total").textContent = fmt(t);
      sums[catalog[r.dataset.id].cat] += t;
    });
    var ex = sums.labour + sums.plant + sums.material;
    var gst = chkGst && chkGst.checked ? ex * gstPct / 100 : 0;
    put("csLabour", fmt(sums.labour)); put("csPlant", fmt(sums.plant)); put("csMaterial", fmt(sums.material));
    put("csEx", fmt(ex)); put("csGst", fmt(gst)); put("csInc", fmt(ex + gst));
    put("csMarkup", String(markup)); put("csGstPct", String(gstPct));
    put("csShare", lump && ex ? "That is " + (ex / lump * 100).toFixed(2) + "% of the base contract (" + fmt(lump) + ")." : "");
    if (noMsg) noMsg.classList.toggle("d-none", rows().length > 0);
  }

  function buildRow(it, st) {
    var li = el("li", "cost-row"); li.dataset.id = it.id; li.dataset.cat = it.cat;
    li.appendChild(hidden("cost_item", it.id));
    var name = el("div", "cr-name"); name.appendChild(el("strong", "", it.desc));
    var small = el("small", "cr-rate", rateText(it, st.type)); name.appendChild(small); li.appendChild(name);

    var qtyWrap = el("div", "cost-in");
    var q = document.createElement("input");
    q.type = "number"; q.name = "cost_qty"; q.className = "form-control cost-qty"; q.min = "0"; q.step = "1";
    q.placeholder = /^day/i.test(it.uom) ? "Days" : "Qty"; q.setAttribute("inputmode", "numeric"); q.value = st.qty || "";
    q.setAttribute("aria-label", "Quantity for " + it.desc);
    qtyWrap.appendChild(q); qtyWrap.appendChild(el("span", "unit", unitWord(it)));

    if (it.cat === "material") {
      li.appendChild(hidden("cost_type", ""));
      var uw = el("div", "cost-in"); uw.appendChild(el("span", "unit", "$"));
      var u = document.createElement("input");
      u.type = "number"; u.name = "cost_unit"; u.className = "form-control cost-unit-in"; u.min = "0"; u.step = "1";
      u.placeholder = "Unit cost"; u.setAttribute("inputmode", "numeric"); u.value = st.unit || "";
      u.setAttribute("aria-label", "Unit cost for " + it.desc);
      uw.appendChild(u); li.appendChild(uw);
    } else {
      var type = st.type === "standby" ? "standby" : "working";
      var tg = el("div", "cost-type"); tg.setAttribute("role", "group"); tg.setAttribute("aria-label", "Rate type");
      tg.appendChild(hidden("cost_type", type));
      ["working", "standby"].forEach(function (t) {
        var b = el("button", "", t === "working" ? "Working" : "Standby"); b.type = "button"; b.dataset.t = t;
        b.setAttribute("aria-pressed", String(t === type)); tg.appendChild(b);
      });
      li.appendChild(tg);
      li.appendChild(hidden("cost_unit", ""));
    }
    li.appendChild(qtyWrap);
    li.appendChild(el("div", "cr-total", "$0.00"));
    var rm = el("button", "cost-remove"); rm.type = "button"; rm.title = "Remove"; rm.setAttribute("aria-label", "Remove " + it.desc);
    var ic = el("i", "bi bi-x-lg"); ic.setAttribute("aria-hidden", "true"); rm.appendChild(ic); li.appendChild(rm);
    return li;
  }

  function addRow(id, st) {
    var it = catalog[id]; if (!it) return null;
    var li = buildRow(it, st || { type: "working", qty: "", unit: "" });
    list.appendChild(li);
    syncSameQuantities(); // Apply same hours if switch is active
    recalc();             // Trigger recalculation on add
    return li;
  }

  /* ---- events on the rows ---- */
  list.addEventListener("click", function (e) {
    // 1. Working / Standby toggle trigger
    var tb = e.target.closest ? e.target.closest(".cost-type button") : null;
    if (tb) {
      var row = tb.closest("li.cost-row"), hid = row.querySelector('input[name="cost_type"]');
      hid.value = tb.dataset.t;
      Array.prototype.forEach.call(row.querySelectorAll(".cost-type button"), function (b) { b.setAttribute("aria-pressed", String(b === tb)); });
      row.querySelector(".cr-rate").textContent = rateText(catalog[row.dataset.id], hid.value);
      recalc(); save(); return;
    }
    // 2. Remove row trigger
    var rb = e.target.closest ? e.target.closest(".cost-remove") : null;
    if (!rb) return;
    var r = rb.closest("li.cost-row"), id = r.dataset.id;
    r.remove();
    if (picker && catalog[id] && catalog[id].active && !picker.options[id]) picker.addOption(pickerOption(catalog[id]));
    syncSameQuantities();
    recalc(); save();
  });

  // 3. Hour input / Unit cost change trigger
  list.addEventListener("input", function (e) {
    if (e.target.matches(".cost-qty, .cost-unit-in")) {
      if (e.target.classList.contains("cost-qty") && chkSameQty && chkSameQty.checked) {
        setAllQuantities(e.target.value, e.target);
      }
      recalc(); 
    }
  });

  // 4. "Apply same hours" switch change trigger
  if (chkSameQty) {
    chkSameQty.addEventListener("change", function () {
      if (chkSameQty.checked) {
        syncSameQuantities();
      }
      recalc();
      save();
    });
  }

  // 5. GST checkbox change trigger
  if (chkGst) chkGst.addEventListener("change", function () { recalc(); save(); });

  /* 6. "No cost impact" switch change trigger */
  function applyNoCost(on) {
    if (pickerWrap) pickerWrap.classList.toggle("d-none", on);
    if (body) body.classList.toggle("d-none", on);
  }
  if (chkNo) chkNo.addEventListener("change", function () {
    if (!chkNo.checked) { applyNoCost(false); recalc(); save(); return; }
    function clearAndHide() {
      rows().forEach(function (r) {
        var id = r.dataset.id; r.remove();
        if (picker && catalog[id] && catalog[id].active && !picker.options[id]) picker.addOption(pickerOption(catalog[id]));
      });
      recalc(); applyNoCost(true); save();
    }
    if (!rows().length) { clearAndHide(); return; }
    var ask = window.WPU && WPU.confirm
      ? WPU.confirm("The estimate lines are removed.", { title: "No cost impact?", ok: "Remove them" })
      : Promise.resolve(window.confirm("Remove the estimate lines?"));
    ask.then(function (ok) { if (ok) clearAndHide(); else chkNo.checked = false; });
  });

  /* ---- the searchable picker, grouped Labour / Plant / Materials ---- */
  var GROUPS = [{ value: "labour", label: "Labour" }, { value: "plant", label: "Construction plant" }, { value: "material", label: "Materials & preliminaries" }];
  function pickerOption(it) {
    return { value: String(it.id), cat: it.cat, no: it.no, desc: it.desc, uom: it.uom, w: it.w, s: it.s, text: it.no + " " + it.desc };
  }
  function priceHint(d) {
    if (d.cat === "material") return "cost + " + markup + "%";
    var w = parseFloat(d.w) || 0, s = parseFloat(d.s) || 0;
    return "Working " + fmt(w) + " \u00b7 Standby " + fmt(s) + " / " + d.uom;
  }
  var inUse = {};
  data.lines.forEach(function (l) { inUse[l.item] = true; });

  if (sel && window.TomSelect && data.editable) {
    sel.innerHTML = "";
    var opts = data.catalog.filter(function (i) { return i.active && !inUse[i.id]; }).map(pickerOption);
    picker = new TomSelect(sel, {
      options: opts, optgroups: GROUPS, optgroupField: "cat", lockOptgroupOrder: true,
      valueField: "value", labelField: "text", searchField: ["text"],
      maxItems: null, hideSelected: false, closeAfterSelect: false, openOnFocus: true, maxOptions: null,
      dropdownParent: "body",
      placeholder: sel.getAttribute("placeholder") || "Search...",
      render: {
        option: function (d, esc) { return '<div class="pk-opt"><strong>' + esc(d.no) + " " + esc(d.desc) + "</strong><span>" + esc(priceHint(d)) + "</span></div>"; },
        optgroup_header: function (d, esc) { return '<div class="optgroup-header">' + esc(d.label) + "</div>"; },
        no_results: function () { return '<div class="no-results">Nothing matches that search</div>'; }
      },
      onInitialize: function() {
        var self = this;
        self.positionDropdown = function() {
          if (!self.isOpen) return;
          var controlRect = self.control.getBoundingClientRect();
          var dropdownRect = self.dropdown.getBoundingClientRect();
          var top = controlRect.top + window.pageYOffset - dropdownRect.height - 4;
          var left = controlRect.left + window.pageXOffset;
          
          self.dropdown.style.width  = controlRect.width + 'px';
          self.dropdown.style.top    = top + 'px';
          self.dropdown.style.left   = left + 'px';
        };
      },
      onItemAdd: function (value) {
        var self = this;
        addRow(value);
        self.removeItem(value, true);
        self.removeOption(value, true);
        self.setTextboxValue(""); 
        self.refreshOptions(false);
        save();
        
        setTimeout(function () {
          self.open();
          if (self.control_input) {
            self.control_input.focus();
          } else {
            self.focus();
          }
        }, 10);
      }
    });
  } else if (sel) {
    sel.disabled = true;
  }

  /* ---- start: the saved lines, then the totals ---- */
  data.lines.forEach(function (l) { addRow(l.item, { type: l.type, qty: l.qty, unit: l.unit }); });
  applyNoCost(!!(chkNo && chkNo.checked));
  recalc();
})();

/* ---------------------------------------------------------- Auto-expanding Textareas */
  var MIN_TEXTAREA_HEIGHT = 60; // Minimum height in pixels for textareas

  document.addEventListener("input", function (e) {
    var t = e.target;
    if (t.tagName === "TEXTAREA") {
      // Temporarily shrink height to compute true scrollHeight accurately
      t.style.height = "auto";
      // Set new height matching the content size, bounded by minimum height
      t.style.height = Math.max(t.scrollHeight + 2, MIN_TEXTAREA_HEIGHT) + "px";
    }
  });

  // Also initialize height and min-height on load for all textareas
  $$("textarea").forEach(function (ta) {
    ta.style.overflow = "hidden"; // Hide internal scrollbars
    ta.style.resize = "none";     // Disable manual resizing drag-handle which messes up layout
    ta.style.minHeight = MIN_TEXTAREA_HEIGHT + "px";
    if (ta.value) {
      ta.style.height = "auto";
      ta.style.height = Math.max(ta.scrollHeight + 2, MIN_TEXTAREA_HEIGHT) + "px";
    } else {
      ta.style.height = MIN_TEXTAREA_HEIGHT + "px";
    }
  });

  /* ---------------------------------------------------------- dirty tracking, conditional fields */
  function evalShow(form) {
    $$("[data-show-when]", form).forEach(function (el) {
      // "name=a|b" shows when name is a or b; join several with " and " (all must match)
      var ok = el.dataset.showWhen.split(" and ").every(function (cond) {
        var p = cond.split("="), ctl = form.elements[p[0].trim()];
        return p[1].trim().split("|").indexOf(ctl ? ctl.value : "") !== -1;
      });
      el.classList.toggle("d-none", !ok);
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
  /* step 2: type chips (counter, Clear all, Other box, HSE reminder) */
  (function () {
    var f = panel(2); if (!f) return;
    var boxes = $$('input[name="event_types"]', f);
    if (!boxes.length) return;
    var count = $("#typeCount", f), clear = $("#typeClear", f),
        hse = $("#hseNote", f), other = $("#typeOtherWrap", f);
    function any(list, attr) { return list.some(function (b) { return b.hasAttribute(attr); }); }
    function sync() {
      var on = boxes.filter(function (b) { return b.checked; });
      if (count) {
        count.textContent = on.length ? on.length + " selected" : "Nothing ticked yet. Tick every type that applies.";
        count.classList.toggle("has", on.length > 0);
      }
      if (clear) clear.classList.toggle("d-none", !on.length);
      $$(".type-group", f).forEach(function (g) {
        var n = $$("input:checked", g).length, badge = $("[data-n]", g);
        if (badge) badge.textContent = n || "";
      });
      if (hse) hse.classList.toggle("d-none", !any(on, "data-hse"));
      if (other) other.classList.toggle("d-none", !any(on, "data-other"));
    }
    f.addEventListener("change", function (e) { if (e.target.name === "event_types") sync(); });
    if (clear) clear.addEventListener("click", function () {
      var ticked = boxes.filter(function (b) { return b.checked; });
      if (!ticked.length) return;
      ticked.forEach(function (b) { b.checked = false; });
      ticked[ticked.length - 1].dispatchEvent(new Event("change", { bubbles: true }));   // saves and refreshes the page bits
      boxes[0].focus();
    });
    sync();
  })();
  /* step 3: HSE reminder (follows step 2) and the "Cause not known yet" switch */
  (function () {
    var f = panel(3); if (!f) return;

    var hint = $("#hseHint", f);
    function syncHse() {
      var on = $$('input[name="event_types"][data-hse]').some(function (b) { return b.checked; });
      if (hint) hint.classList.toggle("d-none", !on);
    }
    document.addEventListener("change", function (e) { if (e.target.name === "event_types") syncHse(); });
    syncHse();

    var cause = $('[name="cause"]', f), sw = $('[name="cause_unknown"]', f);
    if (!cause || !sw) return;
    var wrap = sw.closest(".wpu-switch"), ph = cause.getAttribute("placeholder") || "";
    function syncCause() {
      var has = cause.value.trim() !== "";
      if (has && sw.checked) sw.checked = false;            // typing a cause replaces "not known"
      sw.disabled = has;                                   // can only be switched on while the box is empty
      if (wrap) { wrap.classList.toggle("is-off", has); wrap.title = has ? "Clear the cause box to use this" : ""; }
      cause.classList.toggle("is-muted", sw.checked);
      cause.placeholder = sw.checked ? "Not known yet" : ph;
    }
    cause.addEventListener("input", function () {
      var was = sw.checked;
      syncCause();
      if (was && !sw.checked) sw.dispatchEvent(new Event("change", { bubbles: true }));   // save the switch too
    });
    sw.addEventListener("change", syncCause);
    syncCause();
  })();

  /* ======================================================================
     Step 5: evidence uploads. Browse, take a photo, or drag and drop. Files go to Cloudinary
     in the background (small ones through the Django views, big ones straight from the
     browser), so the person keeps working. Rename, comment and delete are background requests too.
     ====================================================================== */
  (function () {
    var up = $("#evUploader"), f5 = panel(5);
    if (!up || !f5) return;

    var urls = {
      upload: up.getAttribute("data-upload-url") || "", list: up.getAttribute("data-list-url") || "",
      update: up.getAttribute("data-update-url") || "", del: up.getAttribute("data-delete-url") || "",
      sign: up.getAttribute("data-sign-url") || "", reg: up.getAttribute("data-register-url") || ""
    };
    var MAX_MB = 100, PARALLEL = 3, ID = "999999999";
    var DIRECT_OVER_MB = 8;                     // bigger files go straight to Cloudinary (the server is not in the way)
    var SHRINK_OVER = 1.5 * 1048576, MAX_SIDE = 2560, JPEG_Q = 0.85;
    var list = $("#evList", up), summary = $("#evSummary", up), drop = $("#evDrop", up);
    var fileIn = $("#evFile", up), camIn = $("#evCam", up), pill = $("#evPill"), shrinkSw = $("#evShrink", up);
    var items = [], active = 0, seq = 0;
    var hdr = function () { return { "X-CSRFToken": WPU.csrf, "X-Requested-With": "XMLHttpRequest" }; };

    /* remember the "smaller photos" choice on this device */
    if (shrinkSw) {
      try { if (localStorage.getItem("evShrink") === "0") shrinkSw.checked = false; } catch (e) {}
      shrinkSw.addEventListener("change", function () { try { localStorage.setItem("evShrink", shrinkSw.checked ? "1" : "0"); } catch (e) {} });
    }

    /* ---- small helpers ---- */
    function el(tag, cls, text) {
      var e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text) e.textContent = text;
      return e;
    }
    function icon(cls) { var i = el("i", "bi " + cls); i.setAttribute("aria-hidden", "true"); return i; }
    function fmtSize(b) {
      if (b == null || b === "") return "";
      b = Number(b);
      if (b < 1024) return b + " B";
      if (b < 1048576) return Math.round(b / 1024) + " KB";
      return (b / 1048576).toFixed(b < 10485760 ? 1 : 0) + " MB";
    }
    function extOf(n) { var m = /\.([a-z0-9]+)$/i.exec(n || ""); return m ? m[1].toLowerCase() : ""; }
    var DOCS = ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt"];
    var VIDS = ["mp4", "mov", "m4v", "webm", "avi", "3gp", "mkv"];
    var IMGS = ["jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "bmp"];

    function kindOfAtt(a) {
      var rt = (a.resource_type || "").toLowerCase(), fmt = (a.format || "").toLowerCase();
      if (rt === "video") return "video";
      if (rt === "raw" || DOCS.indexOf(fmt) > -1) return "doc";
      return "image";
    }
    function kindOfFile(f) {
      var t = (f.type || "").toLowerCase(), e = extOf(f.name);
      if (t.indexOf("video/") === 0 || VIDS.indexOf(e) > -1) return "video";
      if (t.indexOf("image/") === 0 || IMGS.indexOf(e) > -1) return "image";
      if (DOCS.indexOf(e) > -1) return "doc";
      return "";
    }
    function kindOf(it) { return it.att ? kindOfAtt(it.att) : kindOfFile(it.file); }
    var ICONS = { image: "bi-image", video: "bi-camera-video", doc: "bi-file-earmark-text" };

    function thumbUrl(a) {
      var u = a.secure_url || "", k = kindOfAtt(a);
      if (u.indexOf("/upload/") < 0) return "";
      if (k === "image") return u.replace("/upload/", "/upload/c_fill,w_160,h_160,q_auto,f_auto/");
      if (k === "video") return u.replace("/upload/", "/upload/c_fill,w_160,h_160,so_0,q_auto/").replace(/\.[a-z0-9]+$/i, ".jpg");
      if ((a.format || "").toLowerCase() === "pdf") return u.replace("/upload/", "/upload/pg_1,c_fill,w_160,h_160,q_auto/").replace(/\.pdf$/i, ".jpg");
      return "";
    }
    function typeText(it) {
      var k = kindOf(it);
      if (k === "image") return "Photo";
      if (k === "video") return "Video";
      return ((it.att ? (it.att.format || "") : extOf(it.file.name)) || "File").toUpperCase();
    }

    /* ---- automatic names: <event no>_<event date>_<counter>, e.g. EV-0012_20261029_01 ---- */
    function pad2(n) { return (n < 10 ? "0" : "") + n; }
    function autoPrefix() {
      var node = document.querySelector('.ev-head [data-live="event_no"]') || document.querySelector('[data-live="event_no"]');
      var no = ((node && node.textContent) || "").trim();
      if (!/^[A-Za-z0-9._-]{1,30}$/.test(no)) no = "EV";
      var p1 = panel(1), de = p1 && p1.elements ? p1.elements["event_date"] : null, dv = de ? de.value : "";
      var stamp;
      if (/^\d{4}-\d{2}-\d{2}$/.test(dv)) { stamp = dv.replace(/-/g, ""); }
      else { var n = new Date(); stamp = "" + n.getFullYear() + pad2(n.getMonth() + 1) + pad2(n.getDate()); }
      return no + "_" + stamp + "_";
    }
    function nextAutoName() {
      var p = autoPrefix(), top = 0;
      items.forEach(function (i) {
        var v = (i.nameEl.value || "").trim();
        if (v.indexOf(p) === 0 && /^\d+$/.test(v.slice(p.length))) top = Math.max(top, parseInt(v.slice(p.length), 10));
      });
      return p + pad2(top + 1);
    }

    function build(it) {
      var li = el("li", "ev-item");
      it.thumb = el("a", "ev-thumb");
      it.thumb.target = "_blank"; 
      it.thumb.rel = "noopener";
      it.thumb.setAttribute("aria-label", "Inspect or download file");
      it.ico = icon("bi-image");
      it.thumb.appendChild(it.ico);

      // Ensure clicking the attachment thumbnail lets the user inspect or download it
      it.thumb.addEventListener("click", function (e) {
        e.stopPropagation();
        var url = (it.att && it.att.secure_url) || it.preview;
        if (!url && it.file && window.URL && URL.createObjectURL) {
          url = URL.createObjectURL(it.file);
        }
        if (url) {
          window.open(url, "_blank");
        } else {
          WPU.toast("info", "File is still uploading or preparing.");
        }
      });

      var body = el("div", "ev-body");
      it.nameEl = el("input", "form-control ev-name");
      it.nameEl.type = "text"; it.nameEl.maxLength = 200; it.nameEl.placeholder = "Name this file";
      it.nameEl.setAttribute("aria-label", "File name"); it.nameEl.setAttribute("autocomplete", "off");
      it.commentEl = el("textarea", "form-control ev-comment");
      it.commentEl.rows = 2; it.commentEl.placeholder = "Add a comment (optional)";
      it.commentEl.setAttribute("aria-label", "Comment");
      it.meta = el("div", "ev-meta");
      it.bar = el("div", "ev-bar d-none");
      it.fill = el("span");
      it.bar.appendChild(it.fill);
      var row = el("div", "ev-row");
      it.msg = el("span", "ev-msg");
      it.retry = el("button", "ev-retry d-none", "Retry");
      it.retry.type = "button";
      row.appendChild(it.msg); row.appendChild(it.retry);
      body.appendChild(it.nameEl); body.appendChild(it.commentEl); body.appendChild(it.meta);
      body.appendChild(it.bar); body.appendChild(row);

      it.delBtn = el("button", "ev-del");
      it.delBtn.type = "button"; 
      it.delBtn.title = "Remove file"; 
      it.delBtn.setAttribute("aria-label", "Remove file");
      it.delBtn.appendChild(icon("bi-trash3"));
      
      // Stop propagation so removing an attachment never triggers draft-level actions
      it.delBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        removeItem(it);
      });

      li.appendChild(it.thumb); li.appendChild(body); li.appendChild(it.delBtn);
      return li;
    }

    function paint(it) {
      var a = it.att, st = it.state, k = kindOf(it);
      it.el.setAttribute("data-state", st);
      it.el.setAttribute("data-kind", k);
      it.ico.className = "bi " + (ICONS[k] || "bi-file-earmark");

      var src = a ? thumbUrl(a) : (it.preview || "");
      var inspectUrl = (a && a.secure_url) || it.preview || "";
      if (inspectUrl) {
        it.thumb.setAttribute("href", inspectUrl);
        it.thumb.setAttribute("title", "Click to inspect or download file");
      } else {
        it.thumb.removeAttribute("href");
        it.thumb.setAttribute("title", "Uploading...");
      }
      if (src !== it.imgSrc) {
        it.imgSrc = src;
        if (it.img) { it.img.remove(); it.img = null; }
        if (src) {
          var im = document.createElement("img");
          im.alt = ""; im.onerror = function () { im.remove(); if (it.img === im) it.img = null; };
          im.src = src;
          it.thumb.appendChild(im); it.img = im;
        }
      }
      var size = a ? a.file_size : (it.sendFile || it.file).size;
      var orig = a ? a.original_filename : it.file.name;
      it.meta.textContent = [typeText(it), fmtSize(size), orig].filter(Boolean).join(" \u00b7 ");
      it.meta.title = orig || "";

      var uploading = st === "uploading", waiting = st === "queued" || st === "starting";
      it.bar.classList.toggle("d-none", !(uploading || waiting));
      it.fill.style.width = Math.round((uploading ? it.progress : 0) * 100) + "%";
      var msg = "";
      if (waiting) msg = "Waiting to upload...";
      else if (uploading) msg = it.note ? it.note : (it.progress >= 1 ? "Finishing..." : "Uploading " + Math.round(it.progress * 100) + "%");
      else if (st === "error") msg = it.error || "Upload failed";
      else if (it.saveMsg) msg = it.saveMsg;
      it.msg.textContent = msg;
      it.msg.className = "ev-msg" + ((st === "error" || it.saveFail) ? " bad" : (it.saveMsg === "Saved" ? " ok" : ""));
      it.retry.classList.toggle("d-none", !((st === "error" && !a) || it.saveFail));
    }

    /* ---- totals, the floating "uploading" badge, and the yes/no + counts the form saves ---- */
    function busyCount() {
      return items.filter(function (i) { return !i.att && (i.state === "queued" || i.state === "starting" || i.state === "uploading"); }).length;
    }
    function syncFields() {
      var img = 0, vid = 0, changed = false;
      items.forEach(function (i) { if (i.att) { var k = kindOfAtt(i.att); if (k === "image") img++; else if (k === "video") vid++; } });
      function set(name, v) {
        var e = f5.elements[name];
        if (e && e.value !== String(v)) { e.value = v; changed = true; }
      }
      set("photos", img ? "yes" : "no"); set("photo_count", img || "");
      set("video", vid ? "yes" : "no"); set("video_count", vid || "");
      if (changed) f5.dispatchEvent(new Event("change", { bubbles: true }));   // autosave
    }
    function refreshAll() {
      var c = { image: 0, video: 0, doc: 0 };
      items.forEach(function (i) { if (i.att) c[kindOfAtt(i.att)]++; });
      var busy = busyCount(), parts = [];
      if (c.image) parts.push(c.image + (c.image === 1 ? " photo" : " photos"));
      if (c.video) parts.push(c.video + (c.video === 1 ? " video" : " videos"));
      if (c.doc) parts.push(c.doc + (c.doc === 1 ? " document" : " documents"));
      if (busy) parts.push(busy + " uploading");
      summary.textContent = parts.join(" \u00b7 ");
      summary.classList.toggle("d-none", !parts.length);
      list.classList.toggle("d-none", !items.length);
      if (pill) {
        var run = items.filter(function (i) { return !i.att && (i.state === "uploading" || i.state === "starting" || i.state === "queued"); });
        if (run.length) {
          var avg = run.reduce(function (s, i) { return s + (i.state === "uploading" ? i.progress : 0); }, 0) / run.length;
          $(".t", pill).textContent = "Uploading " + run.length + (run.length === 1 ? " file" : " files") + "... " + Math.round(avg * 100) + "%";
        }
        pill.classList.toggle("d-none", !run.length);
      }
    }
    window.EvUploads = { busy: busyCount };
    if (pill) pill.addEventListener("click", function () { if (typeof go === "function") go(5); });

    /* ---- requests ---- */
    // one XHR (so there is a progress bar and it can be cancelled); resolves { status, data }
    function xhrPost(it, url, fd, withHeaders, onProgress) {
      return new Promise(function (resolve, reject) {
        var xhr = new XMLHttpRequest();
        it.xhr = xhr;
        xhr.open("POST", url);
        if (withHeaders) { var h = hdr(); Object.keys(h).forEach(function (k) { xhr.setRequestHeader(k, h[k]); }); }
        xhr.upload.onprogress = function (e) { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
        xhr.onload = function () {
          var d = null;
          try { d = JSON.parse(xhr.responseText); } catch (e) {}
          resolve({ status: xhr.status, data: d });
        };
        xhr.onerror = function () { reject({ msg: "No connection. It will retry when you are back online.", net: true }); };
        xhr.ontimeout = function () { reject({ msg: "The upload took too long. Retry on a better connection.", net: true }); };
        xhr.onabort = function () { reject({ aborted: true }); };
        xhr.timeout = 20 * 60 * 1000;
        xhr.send(fd);
      });
    }
    // a small POST to our own server (sign / register); resolves the parsed JSON
    function post(url, obj) {
      return fetch(url, { method: "POST", credentials: "same-origin", headers: hdr(), body: new URLSearchParams(obj) })
        .then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }, function () { return { status: r.status, data: null }; }); },
              function () { throw { msg: "No connection. It will retry when you are back online.", net: true }; });
    }
    function serverMsg(r, fallback) {
      if (!r.data) return "Your session may have expired - log in again, then retry.";
      return r.data.message || fallback;
    }

    /* ---- smaller photos ---- */
    function decode(file) {
      return new Promise(function (resolve, reject) {
        var url = URL.createObjectURL(file), im = new Image();
        im.onload = function () { resolve({ img: im, url: url }); };
        im.onerror = function () { try { URL.revokeObjectURL(url); } catch (e) {} reject(new Error("decode")); };
        im.src = url;
      });
    }
    function prepare(it) {
      var f = it.file;
      var can = shrinkSw && shrinkSw.checked && kindOfFile(f) === "image" && f.size > SHRINK_OVER && !/gif$/i.test(f.type) &&
                window.URL && URL.createObjectURL && document.createElement;
      if (!can) return Promise.resolve(f);
      it.note = "Making the photo smaller..."; paint(it);
      return decode(f).then(function (d) {
        var w = d.img.naturalWidth || d.img.width, h = d.img.naturalHeight || d.img.height;
        var scale = Math.min(1, MAX_SIDE / Math.max(w, h));
        var c = document.createElement("canvas");
        c.width = Math.max(1, Math.round(w * scale)); c.height = Math.max(1, Math.round(h * scale));
        var ctx = c.getContext && c.getContext("2d");
        if (!ctx) { try { URL.revokeObjectURL(d.url); } catch (e) {} return f; }
        ctx.drawImage(d.img, 0, 0, c.width, c.height);
        try { URL.revokeObjectURL(d.url); } catch (e) {}
        return new Promise(function (res) {
          c.toBlob(function (blob) {
            if (!blob || blob.size >= f.size * 0.9) return res(f);          // not worth it: keep the original
            res(new File([blob], f.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg", lastModified: f.lastModified }));
          }, "image/jpeg", JPEG_Q);
        });
      }).catch(function () { return f; }).then(function (r) { it.note = ""; return r; });   // HEIC etc. that this browser cannot read: send as is
    }

    /* ---- upload queue ---- */
    function ensureEvent() {
      if (eventId) return Promise.resolve(true);
      return flushAll().then(function () { return !!eventId; }, function () { return false; });   // saves step 1 first
    }
    function release(it) { if (it.counted) { it.counted = false; active--; } pump(); }
    function fail(it, msg, net) {
      it.state = "error"; it.error = msg; it.net = !!net; it.xhr = null; it.note = "";
      paint(it); release(it); refreshAll();
    }
    function pump() {
      while (active < PARALLEL) {
        var it = items.filter(function (i) { return i.state === "queued"; })[0];
        if (!it) break;
        active++; it.counted = true; it.state = "starting"; paint(it);
        begin(it);
      }
      refreshAll();
    }
    function begin(it) {
      ensureEvent().then(function (ok) {
        if (!it.alive) return;
        if (!ok) { fail(it, "Fill in step 1 first so the report can be saved, then retry.", false); return; }
        it.state = "uploading"; it.progress = 0; paint(it); refreshAll();
        return prepare(it).then(function (file) {
          if (!it.alive) return;
          it.sendFile = file; paint(it);
          var direct = file.size > DIRECT_OVER_MB * 1048576 && urls.sign && urls.reg;
          return (direct ? sendDirect(it, file) : sendViaServer(it, file)).then(function (att) { if (it.alive) done(it, att); });
        });
      }).catch(function (e) { if (!it.alive || (e && e.aborted)) return; fail(it, (e && e.msg) || "Upload failed.", !!(e && e.net)); });
    }
    function progress(it) { return function (p) { it.progress = p; paint(it); refreshAll(); }; }

    // small files: through the Django upload view
    function sendViaServer(it, file) {
      var fd = new FormData();
      fd.append("file", file, file.name);
      fd.append("event_id", eventId);
      fd.append("name", it.nameEl.value.trim());
      fd.append("comment", it.commentEl.value.trim());
      return xhrPost(it, urls.upload, fd, true, progress(it)).then(function (r) {
        if (r.status >= 200 && r.status < 300 && r.data && r.data.status === "success" && r.data.attachment) return r.data.attachment;
        throw { msg: serverMsg(r, "Upload failed.") };
      });
    }

    // big files: ask our server for a signed request, send the file straight to Cloudinary, then tell our server
    function sendDirect(it, file) {
      var step = it.cloud ? Promise.resolve(it.cloud) : post(urls.sign, { event_id: eventId, filename: file.name, size: file.size }).then(function (r) {
        var s = r.data;
        if (!s || s.status !== "success") throw { msg: serverMsg(r, "Could not start the upload.") };
        var fd = new FormData();
        fd.append("file", file, file.name);
        fd.append("api_key", s.api_key); fd.append("timestamp", s.timestamp); fd.append("signature", s.signature); fd.append("folder", s.folder);
        return xhrPost(it, s.upload_url, fd, false, progress(it)).then(function (c) {
          if (c.status >= 200 && c.status < 300 && c.data && c.data.public_id) return c.data;
          throw { msg: (c.data && c.data.error && c.data.error.message) || "Cloudinary did not accept the file." };
        });
      });
      return step.then(function (cloud) {
        it.cloud = cloud;                               // kept, so a retry only repeats the last step
        it.progress = 1; paint(it);
        return post(urls.reg, {
          event_id: eventId, public_id: cloud.public_id, version: cloud.version, signature: cloud.signature,
          resource_type: cloud.resource_type, format: cloud.format || "", bytes: cloud.bytes, secure_url: cloud.secure_url,
          original_filename: it.file.name, name: it.nameEl.value.trim(), comment: it.commentEl.value.trim()
        }).then(function (r) {
          if (r.data && r.data.status === "success" && r.data.attachment) return r.data.attachment;
          throw { msg: serverMsg(r, "The file was uploaded but could not be saved. Tap Retry.") };
        });
      });
    }

    function done(it, att) {
      it.att = att; it.state = "done"; it.xhr = null; it.error = ""; it.note = ""; it.cloud = null;
      if (it.preview) { try { URL.revokeObjectURL(it.preview); } catch (e) {} it.preview = ""; }
      if (!it.nameTouched && it.autoName) it.nameEl.value = it.autoName;               // an older server ignores the name we sent: it is set below
      else if (!it.nameTouched || !it.nameEl.value.trim()) it.nameEl.value = att.name || "";
      if (!it.commentTouched) it.commentEl.value = att.comment || "";
      paint(it); release(it); syncFields(); refreshAll(); refreshProblems();
      flushItem(it);                                                                   // anything still different (a name typed while it uploaded)
    }
    window.addEventListener("online", function () {
      items.forEach(function (i) { if (i.state === "error" && i.net && !i.att) { i.state = "queued"; i.error = ""; paint(i); } });
      pump();
    });

    /* ---- add files ---- */
    function addFiles(fileList) {
      var files = Array.prototype.slice.call(fileList || []), added = 0;
      files.forEach(function (f) {
        if (!kindOfFile(f)) { WPU.toast("warning", f.name + ": only photos, video, PDF and Office files can be added."); return; }
        if (f.size > MAX_MB * 1048576) { WPU.toast("warning", f.name + " is larger than " + MAX_MB + " MB."); return; }
        if (!f.size) { WPU.toast("warning", f.name + " is empty."); return; }
        var dup = items.some(function (i) { var g = i.file || {}; return !i.att && g.name === f.name && g.size === f.size && g.lastModified === f.lastModified; });
        if (dup) { WPU.toast("info", f.name + " is already being added."); return; }
        var it = { key: ++seq, file: f, att: null, state: "queued", progress: 0, alive: true, chain: Promise.resolve() };
        if (kindOfFile(f) === "image" && window.URL && URL.createObjectURL) { try { it.preview = URL.createObjectURL(f); } catch (e) {} }
        it.el = build(it);
        it.autoName = nextAutoName();
        it.nameEl.value = it.autoName;
        items.push(it);
        list.appendChild(it.el);
        paint(it);
        added++;
      });
      if (added) pump();
      refreshAll();
    }
    function pick(input) {
      input.addEventListener("change", function () { addFiles(input.files); input.value = ""; });
    }
  pick(fileIn); pick(camIn);
    
    var browseBtn = $("#evBrowse", up);
    if (browseBtn) {
      browseBtn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        fileIn.click();
      });
    }

    var cameraBtn = $("#evCamera", up);
    if (cameraBtn) {
      cameraBtn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        camIn.click();
      });
    }

    drop.addEventListener("click", function (e) {
      e.stopPropagation();
      if (!e.target.closest("button")) {
        fileIn.click();
      }
    });
    drop.addEventListener("keydown", function (e) {
      if ((e.key === "Enter" || e.key === " ") && e.target === drop) { e.preventDefault(); fileIn.click(); }
    });

    /* drag and drop anywhere on the step (the browser must not open a missed drop) */
    function hasFiles(e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") > -1; }
    ["dragover", "drop"].forEach(function (n) {
      window.addEventListener(n, function (e) { if (hasFiles(e)) e.preventDefault(); });
    });
    f5.addEventListener("dragenter", function (e) { if (hasFiles(e)) drop.classList.add("is-over"); });
    f5.addEventListener("dragover", function (e) { if (hasFiles(e)) { e.preventDefault(); drop.classList.add("is-over"); } });
    f5.addEventListener("dragleave", function (e) { if (!e.relatedTarget || !f5.contains(e.relatedTarget)) drop.classList.remove("is-over"); });
    f5.addEventListener("drop", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault(); drop.classList.remove("is-over");
      addFiles(e.dataTransfer.files);
    });

    /* ---- rename / comment (saved in the background) ---- */
    function scheduleSave(it) { clearTimeout(it.timer); it.timer = setTimeout(function () { flushItem(it); }, 1000); }
    function flushItem(it) {
      clearTimeout(it.timer);
      if (!it.att) return;                                   // not uploaded yet: sent when it is
      var name = it.nameEl.value.trim(), comment = it.commentEl.value.trim(), body = {};
      if (!name) { it.nameEl.value = it.att.name || ""; name = it.att.name || ""; }
      if (name && name !== it.att.name) body.name = name;
      if (comment !== (it.att.comment || "").trim()) body.comment = comment;
      if (!Object.keys(body).length) return;
      it.saveFail = false; it.saveMsg = "Saving..."; paint(it);
      var url = urls.update.replace(ID, it.att.id);
      it.chain = it.chain.then(function () {
        return fetch(url, { method: "POST", credentials: "same-origin", headers: hdr(), body: new URLSearchParams(body) })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d.status !== "success") throw new Error(d.message || "not saved");
            if (d.attachment) Object.assign(it.att, d.attachment);     // a rename changes the Cloudinary link too: use the new one
            it.saveMsg = "Saved"; it.saveFail = false; paint(it);
            setTimeout(function () { if (it.saveMsg === "Saved") { it.saveMsg = ""; paint(it); } }, 2000);
            if (it.nameEl.value.trim() !== it.att.name || it.commentEl.value.trim() !== (it.att.comment || "").trim()) flushItem(it);   // typed more meanwhile
          })
          .catch(function () { it.saveMsg = "Not saved"; it.saveFail = true; paint(it); });
      });
    }

    /* ---- delete ---- */
    function drop1(it) {
      it.alive = false;
      clearTimeout(it.timer);
      items = items.filter(function (i) { return i !== it; });
      if (it.preview) { try { URL.revokeObjectURL(it.preview); } catch (e) {} }
      it.el.remove();
    }
    function askDelete() {
      return WPU.confirm
        ? WPU.confirm("This removes the file from the report.", { title: "Delete this file?", ok: "Delete", icon: "warning" })
        : Promise.resolve(window.confirm("Delete this file?"));
    }
    function removeItem(it) {
      if (!it.att) {                                         // still queued or uploading: just cancel it
        if (it.xhr) { try { it.xhr.abort(); } catch (e) {} }
        drop1(it); release(it); refreshAll();
        return;
      }
      askDelete().then(function (ok) {
        if (!ok) return;
        var next = it.el.nextSibling;                        // hide it now, put it back if the server refuses
        items = items.filter(function (i) { return i !== it; });
        it.el.remove();
        syncFields(); refreshAll();
        fetch(urls.del.replace(ID, it.att.id), { method: "POST", credentials: "same-origin", headers: hdr() })
          .then(function (r) { return r.json(); })
          .then(function (d) { if (d.status !== "success") throw new Error(d.message || "not deleted"); drop1(it); refreshProblems(); })
          .catch(function () {
            list.insertBefore(it.el, next && next.parentNode === list ? next : null);
            items.push(it);
            syncFields(); refreshAll();
            WPU.toast("error", "Could not delete the file. Try again.");
          });
      });
    }

    /* ---- files already saved on this event ---- */
    function addSaved(arr) {
      (arr || []).forEach(function (a) {
        if (items.some(function (i) { return i.att && i.att.id === a.id; })) return;
        var it = { key: ++seq, file: null, att: a, state: "done", progress: 1, alive: true, chain: Promise.resolve() };
        it.el = build(it);
        it.nameEl.value = a.name || ""; it.commentEl.value = a.comment || "";
        items.push(it); list.appendChild(it.el); paint(it);
      });
      refreshAll();
      if (items.length) syncFields();
    }
    function load() {
      if (!eventId) return;
      /* 1. the files the server put in the page: no request, so they show at once */
      var seed = document.getElementById("ev-attachments"), seeded = false;
      if (seed) {
        try { addSaved(JSON.parse(seed.textContent)); seeded = true; }
        catch (e) { if (window.console) console.error("Could not show the saved files", e); }
      }
      /* 2. ask the server again for anything newer (another tab or device) */
      if (!urls.list) return;
      var sep = urls.list.indexOf("?") > -1 ? "&" : "?";
      fetch(urls.list + sep + "event_id=" + encodeURIComponent(eventId), { credentials: "same-origin", headers: hdr() })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.status !== "success") throw new Error("list");
          addSaved(d.attachments);
        })
        .catch(function () { if (!seeded) WPU.toast("error", "Could not load the files for this report."); });
    }
    load();
    refreshAll();
  })();

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
/* ---------------------------------------------------------- Dynamic Activity filtering by Project */
  (function () {
    var f = panel(1); if (!f) return;
    var projSelect = f.elements["project"];
    var actSelect = f.elements["activity"];
    if (!projSelect || !actSelect) return;
    var actCell = actSelect.closest(".fcell");

    // Activity stays disabled until a project is chosen
    function updateActivityState() {
      var ts = actSelect.tomselect;
      if (!projSelect.value) {
        if (ts) {
          ts.clear();
          ts.clearOptions();
          ts.settings.placeholder = "Please choose a project";
          ts.input.setAttribute("placeholder", "Please choose a project");
          ts.refreshOptions(false);
          ts.disable();
        } else {
          actSelect.value = "";
          actSelect.innerHTML = '<option value="">Please choose a project</option>';
          actSelect.disabled = true;
        }
      } else if (ts) {
        ts.settings.placeholder = "Select...";
        ts.input.setAttribute("placeholder", "Select...");
        ts.refreshOptions(false);
        ts.enable();
      } else {
        actSelect.disabled = false;
      }
    }
    function setLoading(on) { if (actCell) actCell.classList.toggle("is-loading", on); }

    projSelect.addEventListener("change", function () {
      var projectId = projSelect.value;
      var hadActivity = !!actSelect.value;

      // Clear current activity value
      actSelect.value = "";
      if (actSelect.tomselect) {
        actSelect.tomselect.clear();
        actSelect.tomselect.clearOptions();
      }

      updateActivityState();
      if (!projectId) return;
      if (hadActivity) WPU.toast("info", "Activity cleared. Pick one for this project.");

      // Fetch filtered activities for the chosen project
      setLoading(true);
      fetch("/events/api/activities/?project_id=" + projectId, {
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" }
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        // If the backend returned a specific message (e.g., empty schedule or project not found)
        if (data.message && (!data.activities || data.activities.length === 0)) {
          Swal.fire({
            icon: "info",
            title: "Activity Schedule",
            text: data.message,
            confirmButtonColor: "#C8102E"
          });
        }

        if (!data.activities) return;

        // Populate Tom Select or standard select
        if (actSelect.tomselect) {
          data.activities.forEach(function (act) {
            actSelect.tomselect.addOption({value: act.id, text: act.label});
          });
          actSelect.tomselect.refreshOptions(false);
        } else {
          // Fallback if Tom Select isn't active
          actSelect.innerHTML = '<option value="">---------</option>';
          data.activities.forEach(function (act) {
            var opt = document.createElement("option");
            opt.value = act.id;
            opt.textContent = act.label;
            actSelect.appendChild(opt);
          });
        }
      })
      .catch(function () {
        Swal.fire({
          icon: "error",
          title: "Connection Error",
          text: "Could not reach the server to load activities. Please check your connection and try again.",
          confirmButtonColor: "#C8102E"
        });
      })
      .then(function () { setLoading(false); });
    });

    // Initial check on load
    updateActivityState();
  })();
  /* step 6: client notification (who picker with "add new", and the Now switch) */
  (function () {
    var f = panel(6); if (!f) return;

    /* who was told: pick from the list, or type a new name and press Enter (saved to the outside-staff list) */
    var whoEl = f.elements["notified_who"];
    if (whoEl && whoEl.tomselect) {
      var ts = whoEl.tomselect;
      ts.settings.create = true;
      ts.settings.createFilter = function (input) { return input.trim().length > 1; };
      ts.settings.render = ts.settings.render || {};
      ts.settings.render.option_create = function (data, escape) {
        return '<div class="create">Add <strong>' + escape(data.input) + "</strong> as a new person</div>";
      };
    }

    /* "Now": fills in the current date and time and locks the picker; untick to type it yourself */
    var now = $("#chkNotifiedNow", f), at = f.elements["notified_at"];
    if (!now || !at) return;
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    function stamp(d) {
      return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
    }
    function lock(on) {
      // lock only the visible box: a disabled real input would be left out of the save
      var fp = at._flatpickr, shown = fp && fp.altInput;
      if (fp) fp.set("clickOpens", !on);
      if (shown) {
        shown.disabled = on;
        shown.classList.toggle("text-muted", on);
        shown.classList.toggle("bg-light", on);
      } else {
        at.readOnly = on;
      }
    }
    now.addEventListener("change", function () {
      lock(now.checked);
      if (!now.checked) return;
      var d = new Date();
      if (at._flatpickr) at._flatpickr.setDate(d, true);      // true = fire change, so it autosaves
      else { at.value = stamp(d); at.dispatchEvent(new Event("change", { bubbles: true })); }
    });
  })();

  /* ---------------------------------------------------------- "still missing" column
     The server decides what is missing (Event.problems) and sends the list with every save, so the page
     never has its own copy of the rules. The column opens when a submit check fails, then each item
     disappears as soon as the saved answer fixes it. */
  var lastProblems = cfg.problems || [], checkOpen = false, refreshTimer = null;
  var checkEl = $("#evCheck"), checkBody = $("#evCheckBody"), checkN = $("#evCheckN"),
      checkOk = $("#evCheckOk"), checkHead = $("#evCheckHead");

  function stepTitle(n) { var t = $('#stepper li[data-step="' + n + '"] .t'); return t ? t.textContent : "Step " + n; }

  function groupFor(step) {
    var g = $('.ev-check-group[data-step="' + step + '"]', checkBody);
    if (g) return g;
    g = document.createElement("div"); g.className = "ev-check-group"; g.dataset.step = step;
    var h = document.createElement("div"); h.className = "ev-check-gh";
    var n = document.createElement("span"); n.className = "n"; n.textContent = step;
    var t = document.createElement("span"); t.textContent = stepTitle(step);
    h.appendChild(n); h.appendChild(t); g.appendChild(h);
    var after = $$(".ev-check-group", checkBody).filter(function (x) { return +x.dataset.step > step; })[0];
    checkBody.insertBefore(g, after || null);
    return g;
  }
  function dismiss(b) {
    b.classList.add("is-done");
    b._t = setTimeout(function () {
      var g = b.parentNode; b.remove();
      if (g && !$(".ev-check-item", g)) g.remove();
    }, 450);
  }
  function renderChecklist() {
    if (!checkEl) return;
    var want = {}, have = {};
    lastProblems.forEach(function (p) { want[p.id] = p; });
    $$(".ev-check-item", checkBody).forEach(function (b) {
      if (want[b.dataset.id]) { have[b.dataset.id] = true; clearTimeout(b._t); b.classList.remove("is-done"); }
      else if (!b.classList.contains("is-done")) dismiss(b);          // fixed: fade it out
    });
    lastProblems.forEach(function (p) {
      if (have[p.id]) return;
      var b = document.createElement("button");
      b.type = "button"; b.className = "ev-check-item";
      b.dataset.id = p.id; b.dataset.step = p.step; b.dataset.field = p.field;
      var i = document.createElement("i"); i.className = "bi bi-circle"; i.setAttribute("aria-hidden", "true");
      var t = document.createElement("span"); t.textContent = p.msg;
      b.appendChild(i); b.appendChild(t);
      groupFor(p.step).appendChild(b);
    });
    checkN.textContent = lastProblems.length || "\u2713";            // a tick when nothing is left, also when minimised
    checkN.classList.toggle("text-bg-danger", lastProblems.length > 0);
    checkN.classList.toggle("text-bg-success", lastProblems.length === 0);
    checkOk.classList.toggle("d-none", lastProblems.length > 0);
  }
  function setProblems(list) {
    lastProblems = list || [];
    if (checkOpen) renderChecklist();
  }
  function openChecklist() {
    if (!checkEl) return;
    checkOpen = true;
    checkEl.classList.remove("d-none"); setMin(false);
    renderChecklist();
  }
  function setMin(min) {                       // minimise to just the title bar (and back)
    if (!checkEl) return;
    checkEl.classList.toggle("is-min", min);
    if (checkHead) checkHead.setAttribute("aria-expanded", String(!min));
  }
  if (checkHead) checkHead.addEventListener("click", function () { setMin(!checkEl.classList.contains("is-min")); });
  function closeChecklist() {
    checkOpen = false;
    if (!checkEl) return;
    checkEl.classList.add("d-none");
  }
  /* files are saved by their own views, so ask for a fresh list after one is added or removed */
  function refreshProblems() {
    if (!eventId || !checkOpen) return;
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(function () { enqueue(5, true); }, 300);
  }

  function focusField(n, field) {
    var f = panel(n); if (!f) return;
    var el = null;
    if (field === "attachments") el = $("#evDrop", f);
    else if (field === "cost_lines") el = $$(".cost-qty, .cost-unit-in", f).filter(function (i) { return !i.value.trim() || +i.value <= 0; })[0] || $("#costPicker", f);
    else if (field === "followups") el = $$("#fuRows input, #fuRows textarea, #fuRows select", f).filter(function (i) { return !i.value.trim(); })[0] || $("#fuAdd", f);
    else if (field === "plant_delays") el = $$(".plant-hours", f).filter(function (i) { return !i.value.trim(); })[0] || $("#plantPicker", f);
    else {
      el = f.elements[field];
      if (el && !el.tagName && el.length) el = el[0];                  // a group of radios or ticks
    }
    if (!el) return;
    var cell = (el.closest && el.closest(".fcell")) || el;
    if (el.tomselect) el.tomselect.focus();
    else if (el.focus && el.type !== "hidden") { try { el.focus({ preventScroll: true }); } catch (e) {} }
    if (cell.scrollIntoView) cell.scrollIntoView({ behavior: "smooth", block: "center" });
    cell.classList.remove("ev-flash"); void cell.offsetWidth; cell.classList.add("ev-flash");
    setTimeout(function () { cell.classList.remove("ev-flash"); }, 1700);
  }
  function jump(n, field) {
    var ready = n === current ? Promise.resolve("ok") : enqueue(current).then(function (r) {
      if (r === "invalid") { WPU.toast("warning", "Fix the highlighted fields first."); return r; }
      show(n, true);
      if (eventId) enqueue(n, true);
      return r;
    });
    ready.then(function (r) { if (r !== "invalid") setTimeout(function () { focusField(n, field); }, 80); });
  }
  if (checkBody) checkBody.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".ev-check-item");
    if (b && !b.classList.contains("is-done")) jump(+b.dataset.step, b.dataset.field);
  });
  var checkClose = $("#evCheckClose"); if (checkClose) checkClose.addEventListener("click", closeChecklist);

  /* a failed check: open the column and take the person to the first thing to fix */
  function showMissing() {
    openChecklist();
    var first = lastProblems.filter(function (p) { return p.step === current; })[0] || lastProblems[0];
    if (!first) return;
    var n = lastProblems.length;
    WPU.toast("warning", n + (n === 1 ? " thing" : " things") + " still need filling in. See the checklist.");
    jump(first.step, first.field);
  }

  /* ---------------------------------------------------------- steps */
  function show(n, silent) {
    current = n;
    $$("form.step-panel").forEach(function (f) { f.classList.toggle("d-none", +f.dataset.step !== n); });
    
    // Recalculate textareas in the newly visible panel (prevents 0-height when evaluated while hidden)
    var activePanel = panel(n);
    if (activePanel) {
      $$("textarea", activePanel).forEach(function (ta) {
        ta.style.height = "auto";
        ta.style.height = Math.max(ta.scrollHeight + 2, MIN_TEXTAREA_HEIGHT) + "px";
      });
    }

    $$("#stepper li").forEach(function (li) {
      var k = +li.dataset.step;
      li.classList.toggle("active", k === n); li.classList.toggle("done", k < n);
    });
    var idx = steps.indexOf(n);
    $("#stepLabel").textContent = "Step " + (idx + 1) + " of " + steps.length;
    var back = $("#btnBack"); back.disabled = idx === 0; back.classList.toggle("invisible", idx === 0);
    var nextBtn = $("#btnNext"), nextT = $('#stepper li[data-step="' + steps[idx + 1] + '"] .t');
    nextBtn.innerHTML = "Next" + (nextT ? '<span class="d-none d-sm-inline">: ' + esc(nextT.textContent) + "</span>" : "") + ' <i class="bi bi-arrow-right"></i>';
    nextBtn.classList.toggle("d-none", n === last || !!cfg.review);
    if (cfg.review) back.classList.add("d-none");
    var tb = $("#aiToolbar"), pn = panel(n);
    if (tb) tb.classList.toggle("d-none", !(pn && pn.querySelector("textarea, #fuAdd")));
    var sb = $("#btnSubmit"); if (sb) sb.classList.toggle("d-none", n !== last);
    history.replaceState(null, "", location.pathname + location.search + "#step-" + n);
    var top = $("#evTop"); if (!silent && top && top.scrollIntoView) top.scrollIntoView({ behavior: "smooth", block: "start" });
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
  function sendSubmit() {
    return fetch(cfg.urls.submit.replace("{id}", eventId), {
      method: "POST", credentials: "same-origin",
      headers: { "X-CSRFToken": WPU.csrf, "X-Requested-With": "XMLHttpRequest" }
    }).then(function (resp) { return resp.json(); }).then(function (d) {
      if (d.ok) { window.location = d.redirect; return; }
      if (d.problems && d.problems.length) { setProblems(d.problems); showMissing(); return; }   // the server found something the page had not
      Swal.fire({ icon: "error", title: "Not submitted", confirmButtonColor: "#C8102E",
        text: d.error || (d.summary || []).join(" ") || "Could not submit. Try again." });
    }).catch(function () {
      WPU.toast("error", "Could not reach the server or your session expired. Your answers are saved - try again.");
    });
  }
  var sub = $("#btnSubmit");
  if (sub) sub.addEventListener("click", function () {
    if (window.EvUploads && window.EvUploads.busy()) { WPU.toast("warning", "Files are still uploading. Submit when they have finished."); return; }
    flushAll().then(function (r) {
      if (r === "invalid") { WPU.toast("warning", "Fix the highlighted fields first."); return; }
      if (r === "offline") { WPU.toast("warning", "You are offline. Try again when you have signal."); return; }
      if (!eventId) { WPU.toast("info", "Nothing to submit yet. Fill in the form first."); return; }
      // a forced save answers with the up-to-date list of what is still missing
      return enqueue(current, true).then(function (r2) {
        if (r2 !== "ok") { WPU.toast("warning", r2 === "offline" ? "You are offline. Try again when you have signal." : "Fix the highlighted fields first."); return; }
        if (lastProblems.length) { showMissing(); return; }
        if (checkOpen) renderChecklist();                                  // shows "all done"
        var ask = window.EvReview ? window.EvReview.confirm()
          : WPU.confirm("It will move to Finished. You can still reopen it, and every change is logged.",
              { title: "Submit this event?", ok: "Submit" });
        return ask.then(function (ok) { if (ok) return sendSubmit(); });
      });
    });
  });

  /* ---------------------------------------------------------- read only (a submitted report)
     The server refuses changes anyway (403); this just makes the page match: fields cannot be edited,
     pickers are switched off, and files can still be opened. Runs again when the page adds rows or pickers. */
  function lockPanels() {
    locked.forEach(function (n) {
      var f = panel(n); if (!f) return;
      $$("input, select, textarea, .btn-remove-plant, #typeClear, #fuAdd, #evUploader button, .cost-type button, .cost-remove", f).forEach(function (el) {
        if (el.tomselect) { try { el.tomselect.disable(); } catch (e) {} }
        if (!el.disabled) el.disabled = true;
      });
    });
  }
  if (locked.length) {
    lockPanels();
    new MutationObserver(lockPanels).observe(root, { childList: true, subtree: true });
    setState("saved", "Read only");
  }

  /* ---------------------------------------------------------- process (Construction Manager) */
  function sendProcess() {
    return fetch(cfg.urls.process.replace("{id}", eventId), {
      method: "POST", credentials: "same-origin",
      headers: { "X-CSRFToken": WPU.csrf, "X-Requested-With": "XMLHttpRequest" }
    }).then(function (resp) { return resp.json(); }).then(function (d) {
      if (d.ok) { window.location = d.redirect; return; }
      if (d.problems && d.problems.length) { setProblems(d.problems); showMissing(); return; }
      Swal.fire({ icon: "error", title: "Not processed", confirmButtonColor: "#C8102E",
        text: d.error || (d.summary || []).join(" ") || "Could not process. Try again." });
    }).catch(function () {
      WPU.toast("error", "Could not reach the server or your session expired. Your answers are saved - try again.");
    });
  }
  var proc = $("#btnProcess");
  if (proc) proc.addEventListener("click", function () {
    flushAll().then(function (r) {
      if (r === "invalid") { WPU.toast("warning", "Fix the highlighted fields first."); return; }
      if (r === "offline") { WPU.toast("warning", "You are offline. Try again when you have signal."); return; }
      // a forced save of step 7 answers with the up-to-date list of what is still missing
      return enqueue(7, true).then(function (r2) {
        if (r2 !== "ok") { WPU.toast("warning", r2 === "offline" ? "You are offline. Try again when you have signal." : "Fix the highlighted fields first."); return; }
        if (lastProblems.length) { showMissing(); return; }
        if (checkOpen) renderChecklist();
        return WPU.confirm("The report moves to Processed and nobody can change it after that.",
          { title: "Process this report?", ok: "Process" }).then(function (ok) { if (ok) return sendProcess(); });
      });
    });
  });

  /* ---------------------------------------------------------- start */
  var m = /^#step-(\d+)/.exec(location.hash);
  var start = m && steps.indexOf(+m[1]) > -1 ? +m[1] : (steps.indexOf(cfg.start) > -1 ? cfg.start : first);
  show(start, true);                       // no scroll on first load, so the site header stays in view
  if (!eventId && start === first && window.matchMedia && matchMedia("(min-width: 992px)").matches) {
    var t0 = panel(first) && panel(first).elements["event_title"];
    if (t0) t0.focus({ preventScroll: true });   // desktop only: on phones it would pop the keyboard
  }
})();