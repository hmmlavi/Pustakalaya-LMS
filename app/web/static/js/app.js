/* ==========================================================================
   PUSTAKALAYA — Skeuomorphic Micro-Interactions, Theme & Ambience Controller
   Zero external dependencies · Silent by default · prefers-reduced-motion safe
   ========================================================================== */
(function () {
  "use strict";

  var THEME_STORAGE_KEY = "pustakalaya.theme";
  var AMBIENCE_STORAGE_KEY = "pustakalaya.ambience";
  var COOKIE_MAX_AGE = 365 * 24 * 60 * 60;
  var DESKTOP_FIREFLY_COUNT = 14;
  var MOBILE_FIREFLY_COUNT = 6;
  var MINIMAL_FIREFLY_COUNT = 4;

  // Cool moonlit forest firefly palettes (cool-white, pale-green, muted cyan-green)
  var FIREFLY_PALETTES = [
    { core: "#EAF6F3", mid: "#A7C5D5", glow: "rgba(167, 197, 213, 0.28)" },
    { core: "#DFF1E8", mid: "#8AB6A0", glow: "rgba(138, 182, 160, 0.26)" },
    { core: "#DCF0EE", mid: "#7FAEAD", glow: "rgba(127, 174, 173, 0.26)" }
  ];

  var transitionTimer = null;
  var resizeTimer = null;

  function prefersReducedMotion() {
    return Boolean(
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  function systemPrefersNight() {
    return Boolean(
      window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    );
  }

  function writePreferenceCookie(name, value) {
    try {
      var isHttps = Boolean(window.location && window.location.protocol === "https:");
      var inIframe = false;
      try {
        inIframe = window.self !== window.top;
      } catch (_frameErr) {
        inIframe = true;
      }
      var cookieFlags = (isHttps && inIframe)
        ? "; SameSite=None; Secure; Partitioned"
        : ("; SameSite=Lax" + (isHttps ? "; Secure" : ""));
      document.cookie =
        name +
        "=" +
        encodeURIComponent(value) +
        "; path=/; max-age=" +
        COOKIE_MAX_AGE +
        cookieFlags;
    } catch (_err) {
      // Ignore cookie write errors in restricted contexts
    }
  }

  function readThemePref() {
    try {
      var saved = localStorage.getItem(THEME_STORAGE_KEY);
      if (saved === "day" || saved === "night" || saved === "system") {
        return saved;
      }
    } catch (_err) {}
    var attr = document.documentElement.getAttribute("data-theme-pref");
    if (attr === "day" || attr === "night" || attr === "system") {
      return attr;
    }
    return "system";
  }

  function resolveTheme(pref) {
    if (pref === "day" || pref === "night") {
      return pref;
    }
    return systemPrefersNight() ? "night" : "day";
  }

  function readAmbience() {
    try {
      var saved = localStorage.getItem(AMBIENCE_STORAGE_KEY);
      if (saved === "auto" || saved === "minimal" || saved === "off") {
        return saved;
      }
    } catch (_err) {}
    var attr = document.documentElement.getAttribute("data-ambience");
    if (attr === "auto" || attr === "minimal" || attr === "off") {
      return attr;
    }
    return "auto";
  }

  function computeTargetFireflyCount(resolvedTheme, ambience) {
    if (resolvedTheme !== "night" || ambience === "off") {
      return 0;
    }
    if (ambience === "minimal") {
      return MINIMAL_FIREFLY_COUNT;
    }
    var isMobile = window.innerWidth <= 768;
    return isMobile ? MOBILE_FIREFLY_COUNT : DESKTOP_FIREFLY_COUNT;
  }

  function syncFireflies(resolvedTheme, ambience) {
    var layer = document.getElementById("forest-firefly-layer");
    if (!layer) {
      return;
    }
    var targetCount = computeTargetFireflyCount(resolvedTheme, ambience);
    if (targetCount === 0) {
      while (layer.firstChild) {
        layer.removeChild(layer.firstChild);
      }
      layer.dataset.activeCount = "0";
      return;
    }

    if (Number(layer.dataset.activeCount || "0") === targetCount && layer.children.length === targetCount) {
      return;
    }

    while (layer.firstChild) {
      layer.removeChild(layer.firstChild);
    }

    var fragment = document.createDocumentFragment();
    for (var i = 0; i < targetCount; i += 1) {
      var dot = document.createElement("span");
      dot.className = "forest-firefly";

      var palette = FIREFLY_PALETTES[i % FIREFLY_PALETTES.length];
      var edgeBias = i % 2 === 0
        ? (2 + ((i * 7.3) % 20))
        : (78 + ((i * 5.9) % 19));
      var topPct = 8 + ((i * 13.7 + (i % 3) * 11) % 84);
      var sizePx = 2.4 + ((i * 0.42) % 1.9);
      var driftSec = 17 + ((i * 2.4) % 11);
      var pulseSec = 8.5 + ((i * 1.7) % 5.5);
      var delaySec = -1 * ((i * 2.1) % 13);
      var alpha = (0.30 + ((i * 0.05) % 0.22)).toFixed(2);
      var dx = ((i % 2 === 0 ? 1 : -1) * (10 + (i % 8) * 2)) + "px";
      var dy = (-11 - (i % 7) * 3) + "px";

      dot.style.left = edgeBias.toFixed(1) + "%";
      dot.style.top = topPct.toFixed(1) + "%";
      dot.style.width = sizePx.toFixed(1) + "px";
      dot.style.height = sizePx.toFixed(1) + "px";
      dot.style.setProperty("--ff-core", palette.core);
      dot.style.setProperty("--ff-mid", palette.mid);
      dot.style.setProperty("--ff-glow", palette.glow);
      dot.style.setProperty("--ff-drift", driftSec.toFixed(1) + "s");
      dot.style.setProperty("--ff-pulse", pulseSec.toFixed(1) + "s");
      dot.style.setProperty("--ff-delay", delaySec.toFixed(1) + "s");
      dot.style.setProperty("--ff-alpha", alpha);
      dot.style.setProperty("--ff-dx", dx);
      dot.style.setProperty("--ff-dy", dy);
      fragment.appendChild(dot);
    }
    layer.appendChild(fragment);
    layer.dataset.activeCount = String(targetCount);
  }

  function updateAtmosphereControls(pref, resolvedTheme, ambience) {
    var toggles = document.querySelectorAll("[data-theme-toggle]");
    toggles.forEach(function (btn) {
      var isNight = resolvedTheme === "night";
      btn.setAttribute("aria-pressed", isNight ? "true" : "false");
      btn.setAttribute(
        "aria-label",
        isNight
          ? "Moonlit forest dark theme active. Activate to switch to peaceful botanical light theme."
          : "Peaceful botanical light theme active. Activate to switch to moonlit forest dark theme."
      );
      var labelNode = btn.querySelector("[data-horizon-label]");
      if (labelNode) {
        labelNode.textContent = isNight ? "Nightfall" : "Daylight";
      }
    });

    var statusLabels = document.querySelectorAll("[data-reading-atmosphere-text]");
    statusLabels.forEach(function (el) {
      if (resolvedTheme === "night") {
        el.textContent = ambience === "off" ? "Quiet Night Study" : "Lantern-lit Library";
      } else {
        el.textContent = "Sunlit Reading Room";
      }
    });

    var quickAmbBtns = document.querySelectorAll("[data-ambience-cycle]");
    quickAmbBtns.forEach(function (btn) {
      var titleMap = {
        auto: "Ambience: Auto",
        minimal: "Ambience: Minimal",
        off: "Ambience: Off"
      };
      btn.textContent = titleMap[ambience] || "Ambience: Auto";
      btn.setAttribute(
        "aria-label",
        "Library ambience is " + ambience + ". Activate to cycle Auto, Minimal, or Off."
      );
    });

    var themeSelect = document.getElementById("theme_preference");
    if (themeSelect && themeSelect.value !== pref) {
      themeSelect.value = pref;
    }
    var ambSelect = document.getElementById("ambience_mode");
    if (ambSelect && ambSelect.value !== ambience) {
      ambSelect.value = ambience;
    }

    var ambPills = document.querySelectorAll("[data-set-ambience]");
    ambPills.forEach(function (pill) {
      var val = pill.getAttribute("data-set-ambience");
      pill.setAttribute("aria-checked", val === ambience ? "true" : "false");
    });

    var themePills = document.querySelectorAll("[data-set-theme]");
    themePills.forEach(function (pill) {
      var val = pill.getAttribute("data-set-theme");
      pill.setAttribute("aria-checked", val === pref ? "true" : "false");
    });
  }

  function applyThemeAndAmbience(pref, ambience, animateTransition) {
    var root = document.documentElement;
    var resolved = resolveTheme(pref);

    if (animateTransition && !prefersReducedMotion()) {
      root.classList.add("theme-transitioning");
      if (transitionTimer) {
        window.clearTimeout(transitionTimer);
      }
      transitionTimer = window.setTimeout(function () {
        root.classList.remove("theme-transitioning");
        transitionTimer = null;
      }, 620);
    }

    root.setAttribute("data-theme-pref", pref);
    root.setAttribute("data-theme", resolved);
    root.setAttribute("data-ambience", ambience);

    updateAtmosphereControls(pref, resolved, ambience);
    syncFireflies(resolved, ambience);
  }

  function persistThemePreference(pref, animate) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, pref);
    } catch (_err) {}
    writePreferenceCookie("pustakalaya_theme", pref);
    applyThemeAndAmbience(pref, readAmbience(), Boolean(animate));
  }

  function persistAmbiencePreference(ambience) {
    try {
      localStorage.setItem(AMBIENCE_STORAGE_KEY, ambience);
    } catch (_err) {}
    writePreferenceCookie("pustakalaya_ambience", ambience);
    applyThemeAndAmbience(readThemePref(), ambience, false);
  }

  document.addEventListener("DOMContentLoaded", function () {
    // 1. Initialize Theme, Ambience & Moonlit Fireflies
    var currentPref = readThemePref();
    var currentAmb = readAmbience();
    applyThemeAndAmbience(currentPref, currentAmb, false);

    if (window.matchMedia) {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      var onSchemeChange = function () {
        if (readThemePref() === "system") {
          applyThemeAndAmbience("system", readAmbience(), true);
        }
      };
      if (typeof mq.addEventListener === "function") {
        mq.addEventListener("change", onSchemeChange);
      }
    }

    window.addEventListener("resize", function () {
      if (resizeTimer) {
        window.clearTimeout(resizeTimer);
      }
      resizeTimer = window.setTimeout(function () {
        syncFireflies(resolveTheme(readThemePref()), readAmbience());
      }, 240);
    });

    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var nowResolved = resolveTheme(readThemePref());
        var nextTheme = nowResolved === "night" ? "day" : "night";
        persistThemePreference(nextTheme, true);
      });
    });

    document.querySelectorAll("[data-ambience-cycle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var order = ["auto", "minimal", "off"];
        var cur = readAmbience();
        var next = order[(order.indexOf(cur) + 1) % order.length];
        persistAmbiencePreference(next);
      });
    });

    document.querySelectorAll("[data-set-theme]").forEach(function (pill) {
      pill.addEventListener("click", function () {
        var targetPref = pill.getAttribute("data-set-theme") || "system";
        persistThemePreference(targetPref, true);
      });
    });

    document.querySelectorAll("[data-set-ambience]").forEach(function (pill) {
      pill.addEventListener("click", function () {
        var targetAmb = pill.getAttribute("data-set-ambience") || "auto";
        persistAmbiencePreference(targetAmb);
      });
    });

    var appearanceForm = document.getElementById("appearance-preferences-form");
    if (appearanceForm) {
      appearanceForm.addEventListener("submit", function () {
        var tSel = document.getElementById("theme_preference");
        var aSel = document.getElementById("ambience_mode");
        if (tSel) {
          try { localStorage.setItem(THEME_STORAGE_KEY, tSel.value); } catch (_e) {}
        }
        if (aSel) {
          try { localStorage.setItem(AMBIENCE_STORAGE_KEY, aSel.value); } catch (_e) {}
        }
      });
    }

    // 2. Responsive Mobile Sidebar Drawer + Backdrop + Escape key
    var menuBtn = document.getElementById("mobile-menu-toggle");
    var sidebar = document.getElementById("app-sidebar");
    var backdrop = document.getElementById("sidebar-backdrop");

    function setSidebarOpen(open) {
      if (!sidebar || !menuBtn) {
        return;
      }
      sidebar.classList.toggle("open", open);
      menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
      if (backdrop) {
        backdrop.classList.toggle("visible", open);
      }
    }

    if (menuBtn && sidebar) {
      menuBtn.addEventListener("click", function () {
        setSidebarOpen(!sidebar.classList.contains("open"));
      });
    }
    if (backdrop) {
      backdrop.addEventListener("click", function () {
        setSidebarOpen(false);
      });
    }

    // 3. Smooth Dismissible Notifications (never auto-hides errors prematurely)
    document.querySelectorAll("[data-alert-dismiss]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var alertBox = btn.closest(".alert");
        if (!alertBox) {
          return;
        }
        if (prefersReducedMotion()) {
          alertBox.remove();
          return;
        }
        alertBox.classList.add("is-dismissing");
        window.setTimeout(function () {
          alertBox.remove();
        }, 175);
      });
    });

    // 4. Accessible Confirmation Dialog with Smooth Fade & Focus Restoration
    var modal = document.getElementById("pustakalaya-confirm-modal");
    var modalMessage = document.getElementById("pustakalaya-confirm-message");
    var modalCancel = document.getElementById("pustakalaya-confirm-cancel");
    var modalAccept = document.getElementById("pustakalaya-confirm-accept");
    var pendingForm = null;
    var previousFocus = null;

    function closeConfirmModal() {
      if (!modal) {
        return;
      }
      var focusTarget = previousFocus;
      pendingForm = null;
      previousFocus = null;

      if (prefersReducedMotion()) {
        modal.classList.remove("is-closing");
        modal.hidden = true;
        if (focusTarget && typeof focusTarget.focus === "function") {
          focusTarget.focus();
        }
        return;
      }

      modal.classList.add("is-closing");
      window.setTimeout(function () {
        modal.classList.remove("is-closing");
        modal.hidden = true;
        if (focusTarget && typeof focusTarget.focus === "function") {
          focusTarget.focus();
        }
      }, 165);
    }

    if (modal && modalCancel && modalAccept && modalMessage) {
      modalCancel.addEventListener("click", closeConfirmModal);
      modal.addEventListener("click", function (evt) {
        if (evt.target === modal) {
          closeConfirmModal();
        }
      });
      modalAccept.addEventListener("click", function () {
        if (!pendingForm) {
          closeConfirmModal();
          return;
        }
        var targetForm = pendingForm;
        targetForm.dataset.confirmed = "true";
        modal.classList.remove("is-closing");
        modal.hidden = true;
        pendingForm = null;
        if (typeof targetForm.requestSubmit === "function") {
          targetForm.requestSubmit();
        } else {
          targetForm.submit();
        }
      });
    }

    document.addEventListener("keydown", function (evt) {
      if (evt.key === "Escape") {
        if (modal && !modal.hidden) {
          evt.preventDefault();
          closeConfirmModal();
          return;
        }
        if (sidebar && sidebar.classList.contains("open")) {
          setSidebarOpen(false);
        }
      }
      if (evt.key === "Tab" && modal && !modal.hidden && modalCancel && modalAccept) {
        if (evt.shiftKey && document.activeElement === modalCancel) {
          evt.preventDefault();
          modalAccept.focus();
        } else if (!evt.shiftKey && document.activeElement === modalAccept) {
          evt.preventDefault();
          modalCancel.focus();
        }
      }
    });

    var confirmForms = document.querySelectorAll("form[data-confirm]");
    confirmForms.forEach(function (form) {
      form.addEventListener("submit", function (evt) {
        if (form.dataset.confirmed === "true") {
          delete form.dataset.confirmed;
          return;
        }
        var msg = form.getAttribute("data-confirm");
        if (!msg) {
          return;
        }
        if (modal && modalMessage && modalCancel) {
          evt.preventDefault();
          pendingForm = form;
          previousFocus = document.activeElement;
          modalMessage.textContent = msg;
          modal.classList.remove("is-closing");
          modal.hidden = false;
          modalCancel.focus();
        } else if (!window.confirm(msg)) {
          evt.preventDefault();
        }
      });
    });

    // 5. Accessible Field Validation Micro-Feedback
    var controls = document.querySelectorAll("input, select, textarea");
    controls.forEach(function (ctrl) {
      ctrl.addEventListener("invalid", function () {
        ctrl.setAttribute("aria-invalid", "true");
      });
      ctrl.addEventListener("input", function () {
        if (ctrl.getAttribute("aria-invalid") === "true" && ctrl.validity && ctrl.validity.valid) {
          ctrl.removeAttribute("aria-invalid");
        }
      });
    });

    // 6. Genuine Loading Indicator & Duplicate-Submit Protection
    var allForms = document.querySelectorAll("form");
    allForms.forEach(function (form) {
      form.addEventListener("submit", function (evt) {
        if (evt.defaultPrevented) {
          return;
        }
        var isPost = (form.method || "").toUpperCase() === "POST";
        if (isPost) {
          if (form.dataset.submitting === "true") {
            evt.preventDefault();
            return;
          }
          form.dataset.submitting = "true";
        }
        var submitter = evt.submitter || form.querySelector('button[type="submit"]');
        if (submitter && submitter.classList) {
          submitter.classList.add("is-submitting");
        }
        if (isPost) {
          window.setTimeout(function () {
            form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach(function (btn) {
              btn.disabled = true;
            });
          }, 0);
        }
      });
    });

    window.addEventListener("pageshow", function () {
      allForms.forEach(function (form) {
        delete form.dataset.submitting;
        delete form.dataset.confirmed;
        form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach(function (btn) {
          btn.disabled = false;
          if (btn.classList) {
            btn.classList.remove("is-submitting");
          }
        });
      });
    });
  });
})();
