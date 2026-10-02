// Small progressive enhancements; every page works without this file.
(function () {
  // "Resend code" countdown on the login page
  function startCountdowns(root) {
    root.querySelectorAll("[data-countdown]").forEach(function (btn) {
      if (btn.dataset.started) return;
      btn.dataset.started = "1";
      var left = parseInt(btn.dataset.countdown, 10) || 0;
      var label = btn.querySelector("[data-left]");
      function tick() {
        if (left <= 0) {
          btn.disabled = false;
          btn.querySelector("[data-wait]").hidden = true;
          btn.querySelector("[data-ready]").hidden = false;
          return;
        }
        btn.disabled = true;
        if (label) label.textContent = left;
        left -= 1;
        setTimeout(tick, 1000);
      }
      tick();
    });
  }

  // Rotating messages while eCourts is being searched (it can take a minute)
  var STEPS = [
    "Connecting to eCourts…",
    "Searching for your case…",
    "eCourts can be slow during court hours. Still working…",
    "Reading hearing dates and orders…",
    "Almost there…",
  ];
  function startProgress(form) {
    var msg = form.querySelector("[data-progress-msg]");
    if (!msg) return;
    var i = 0;
    msg.textContent = STEPS[0];
    clearInterval(form._progress);
    form._progress = setInterval(function () {
      i = Math.min(i + 1, STEPS.length - 1);
      msg.textContent = STEPS[i];
    }, 9000);
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.matches("[data-long-request]")) {
      form.classList.add("busy");
      startProgress(form);
    }
  });

  // Auto-save switches (reminder toggles)
  document.addEventListener("change", function (e) {
    var input = e.target;
    var form = input.closest && input.closest("form[data-autosave]");
    if (form) {
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    }
  });

  // Keep only digits in the OTP box, and submit when 6 are typed
  document.addEventListener("input", function (e) {
    var el = e.target;
    if (el.matches("[data-otp]")) {
      el.value = el.value.replace(/\D/g, "").slice(0, 6);
      if (el.value.length === 6 && el.form && !el.form.querySelector("[name=name]:invalid")) {
        if (el.form.requestSubmit) el.form.requestSubmit();
      }
    }
  });

  document.addEventListener("DOMContentLoaded", function () { startCountdowns(document); });
  document.addEventListener("htmx:afterSettle", function (e) {
    startCountdowns(document);
    document.querySelectorAll("form.busy").forEach(function (f) { f.classList.remove("busy"); clearInterval(f._progress); });
  });
})();

// "Select all" on search results
document.addEventListener("change", function (e) {
  if (e.target.matches("[data-select-all]")) {
    document.querySelectorAll(".hit-check").forEach(function (c) { c.checked = e.target.checked; });
  }
});
