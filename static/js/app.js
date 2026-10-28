/* WPU Site Event Register - wires up the vendor libraries from HTML attributes.
   <select data-ts>                   Tom Select (multi if the select has `multiple`)
   <input data-date|data-time|data-datetime>   Flatpickr (WPU.init(el) for HTML added later)
   <table class="dt">                 DataTables
   <form data-confirm="Submit?" data-guard>   SweetAlert2 confirm / unsaved-changes warning
   <a|button data-confirm="Sure?">    SweetAlert2 confirm before following / submitting */
(function () {
  "use strict";
  var WPU = (window.WPU = window.WPU || {});

  /* ---- SweetAlert2 helpers ---- */
  var accent = "#C8102E";
  WPU.toast = function (icon, title) {
    if (!window.Swal) return;
    Swal.fire({ toast: true, position: "top-end", icon: icon, title: title, showConfirmButton: false, timer: 4000, timerProgressBar: true });
  };
  WPU.confirm = function (text, opts) {
    opts = opts || {};
    return Swal.fire({
      title: opts.title || "Are you sure?", text: text, icon: opts.icon || "question",
      showCancelButton: true, confirmButtonColor: accent, cancelButtonColor: "#6c757d",
      confirmButtonText: opts.ok || "Yes", cancelButtonText: "Cancel", reverseButtons: true,
    }).then(function (r) { return r.isConfirmed; });
  };

  /* Django messages -> toasts */
  var levelIcon = { success: "success", error: "error", warning: "warning", info: "info", debug: "info" };
  document.querySelectorAll("#dj-messages span").forEach(function (s) {
    WPU.toast(levelIcon[s.dataset.level] || "info", s.textContent.trim());
  });

  /* ---- widgets: call WPU.init(rootElement) again after adding HTML dynamically ---- */
  WPU.init = function (scope) {
    scope = scope || document;
    var all = function (sel) { return Array.prototype.slice.call(scope.querySelectorAll(sel)); };

    /* Tom Select */
    all("select[data-ts]").forEach(function (el) {
      if (el.tomselect || !window.TomSelect) return;
      new TomSelect(el, {
        plugins: el.multiple ? ["remove_button"] : [],
        allowEmptyOption: true, create: false, maxOptions: null,
        placeholder: el.getAttribute("placeholder") || "Select...",
      });
    });


/* Flatpickr (phones fall back to the native picker automatically) */
    if (window.flatpickr) {
      var base = { allowInput: true, altInput: true, time_24hr: true, minuteIncrement: 5 };
      var once = function (el, opts) { if (!el._flatpickr) flatpickr(el, Object.assign({}, base, opts)); };
      
      // --- Updated date picker configuration with auto-fill on open ---
      all("input[data-date]").forEach(function (el) { 
        once(el, { 
          dateFormat: "Y-m-d", 
          altFormat: "d M Y",
          onOpen: function(selectedDates, dateStr, instance) {
            // Instantly populates today's date if the field is empty (skipped when the input has data-no-autofill)
            if (!instance.input.hasAttribute("data-no-autofill") && !instance.input.value && !selectedDates.length) {
              instance.setDate(new Date(), true);
            }
          }
        }); 
      });

      // --- Time picker configuration (from before) ---
      all("input[data-time]").forEach(function (el) { 
        once(el, { 
          enableTime: true, 
          noCalendar: true, 
          dateFormat: "H:i",     
          altFormat: "h:i K",    
          time_24hr: false,      
          onOpen: function(selectedDates, dateStr, instance) {
            if (!instance.input.hasAttribute("data-no-autofill") && !instance.input.value && !selectedDates.length) {
              instance.setDate(new Date(), true);
            }
          }
        }); 
      });

      all("input[data-datetime]").forEach(function (el) { once(el, { enableTime: true, dateFormat: "Y-m-d H:i", altFormat: "d M Y H:i" }); });
    }

    /* Tooltips: <i class="tip" data-bs-toggle="tooltip" data-bs-title="..."> (see events/_tip.html) */
    if (window.bootstrap && bootstrap.Tooltip) {
      all('[data-bs-toggle="tooltip"]').forEach(function (el) {
        if (el._tipDone) return;
        el._tipDone = true;
        // own click handler: stops a surrounding <label> grabbing focus, and makes iOS treat the icon as tappable
        el.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); });
        new bootstrap.Tooltip(el, { container: "body", trigger: "hover focus", placement: "top", customClass: "wpu-tip" });
      });
    }

    /* DataTables (v2) */
    if (window.DataTable) {
      all("table.dt").forEach(function (el) {
        if (el.dataset.dtDone) return;
        el.dataset.dtDone = "1";
        new DataTable(el, { pageLength: 25, order: [], responsive: false, language: { search: "", searchPlaceholder: "Search events..." } });
      });
    }
  };
  WPU.init(document);

  /* ---- confirm dialogs ---- */
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (!f.dataset || !f.dataset.confirm || f.dataset.confirmed) return;
    e.preventDefault();
    WPU.confirm(f.dataset.confirm).then(function (ok) {
      if (ok) { f.dataset.confirmed = "1"; f.dirty = false; f.requestSubmit ? f.requestSubmit(e.submitter) : f.submit(); }
    });
  });
  document.addEventListener("click", function (e) {
    var a = e.target.closest("a[data-confirm]");
    if (!a) return;
    e.preventDefault();
    WPU.confirm(a.dataset.confirm).then(function (ok) { if (ok) window.location = a.href; });
  });

  /* ---- unsaved-changes guard ---- */
  document.querySelectorAll("form[data-guard]").forEach(function (f) {
    f.addEventListener("input", function () { f.dirty = true; });
    f.addEventListener("change", function () { f.dirty = true; });
    f.addEventListener("submit", function () { f.dirty = false; });
    window.addEventListener("beforeunload", function (e) { if (f.dirty) { e.preventDefault(); e.returnValue = ""; } });
  });

  /* ---- CSRF header for fetch() calls ---- */
  var m = document.querySelector('meta[name="csrf-token"]');
  WPU.csrf = m ? m.content : "";
})();