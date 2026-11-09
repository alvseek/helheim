/* One press, one call.
 *
 * The tool form's Send button starts a request that can take seconds, and a second
 * press would run the tool a second time — for a tool that writes, that is a real
 * duplicate. So a form marked `data-pending` takes its submit button out of play the
 * moment it is submitted, and shows a spinner until the page moves on.
 */
(function () {
  "use strict";

  function goIdle(element) {
    if (element.dataset.idleHtml === undefined) return;
    element.innerHTML = element.dataset.idleHtml;
    delete element.dataset.idleHtml;
    element.classList.remove("is-busy");
    element.removeAttribute("aria-busy");
    if ("disabled" in element) element.disabled = false;
  }

  function goBusy(element) {
    element.dataset.idleHtml = element.innerHTML;
    var label = element.textContent.trim();
    element.classList.add("is-busy");
    element.setAttribute("aria-busy", "true");
    element.innerHTML = '<span class="spinner" aria-hidden="true"></span>' + label;
    // Disabling a submit button inside its own submit handler has cancelled the
    // submission in some engines, so the disable lands one task after the handler.
    window.setTimeout(function () {
      if ("disabled" in element) element.disabled = true;
    }, 0);
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-pending")) return;
    var button = form.querySelector('button[type="submit"]');
    if (!button) return;
    if (form.dataset.sending) {
      event.preventDefault();
      return;
    }
    form.dataset.sending = "1";
    goBusy(button);
  });

  // A page restored from the back/forward cache keeps the state it was left in, which
  // for this form is a disabled button. Put it back the way the server sent it.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    document.querySelectorAll("[data-idle-html]").forEach(function (element) {
      goIdle(element);
      var form = element.closest("form");
      if (form) delete form.dataset.sending;
    });
  });
})();
