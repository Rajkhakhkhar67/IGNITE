/**
 * Learning Platform — main client-side script (Stage 1)
 *
 * Handles mobile navigation toggle only.
 * Feature logic will be added in later stages.
 */

(function () {
    "use strict";

    const navToggle = document.querySelector(".nav-toggle");
    const primaryNav = document.querySelector("#primary-nav");

    if (navToggle && primaryNav) {
        navToggle.addEventListener("click", function () {
            const isExpanded = navToggle.getAttribute("aria-expanded") === "true";
            navToggle.setAttribute("aria-expanded", String(!isExpanded));
            primaryNav.classList.toggle("is-open", !isExpanded);
        });
    }

    // Close mobile nav when the viewport grows beyond mobile breakpoint
    window.addEventListener("resize", function () {
        if (navToggle && primaryNav && window.innerWidth > 768) {
            navToggle.setAttribute("aria-expanded", "false");
            primaryNav.classList.remove("is-open");
        }
    });

    // Shared non-blocking confirmation for real learning actions. Individual
    // feature scripts call this after their server request succeeds.
    window.lpToast = function (message) {
        let toast = document.querySelector(".lp-toast");
        if (!toast) {
            toast = document.createElement("div");
            toast.className = "lp-toast";
            toast.setAttribute("role", "status");
            toast.setAttribute("aria-live", "polite");
            document.body.appendChild(toast);
        }
        toast.textContent = message;
        toast.classList.remove("is-visible");
        requestAnimationFrame(function () { toast.classList.add("is-visible"); });
        window.clearTimeout(window.lpToastTimer);
        window.lpToastTimer = window.setTimeout(function () {
            toast.classList.remove("is-visible");
        }, 2600);
    };

    // A brief paper-wipe makes ordinary server-rendered navigation feel
    // continuous without intercepting forms, downloads, modified clicks, or
    // browser history. Reduced-motion users retain immediate navigation.
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!reduceMotion) {
        document.querySelectorAll("a[href]").forEach(function (link) {
            link.addEventListener("click", function (event) {
                const url = new URL(link.href, window.location.href);
                const sameDocument = url.href === window.location.href || url.hash;
                const isInternal = url.origin === window.location.origin;
                const isDownload = link.hasAttribute("download") || /\/download(?:\/|$)/.test(url.pathname);
                if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey ||
                    link.target || !isInternal || sameDocument || isDownload) return;
                event.preventDefault();
                document.documentElement.classList.add("page-is-leaving");
                window.setTimeout(function () { window.location.assign(url.href); }, 135);
            });
        });
    }
})();
