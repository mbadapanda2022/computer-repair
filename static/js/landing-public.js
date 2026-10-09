/* static/js/landing-public.js — public portal behaviors */
(function () {
    "use strict";

    // Sticky navbar shadow after scroll
    var navbar = document.querySelector(".public-navbar");
    if (navbar) {
        var onScroll = function () {
            navbar.classList.toggle("is-scrolled", window.scrollY > 10);
        };
        window.addEventListener("scroll", onScroll, { passive: true });
        onScroll();
    }

    // Scroll-reveal sections (progressive enhancement)
    var revealEls = document.querySelectorAll(".reveal");
    if (!revealEls.length) return;

    if (!("IntersectionObserver" in window)) {
        document.documentElement.classList.add("no-reveal-js");
        return;
    }
    var prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (prefersReduced) {
        document.documentElement.classList.add("no-reveal-js");
        return;
    }
    var io = new IntersectionObserver(
        function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    entry.target.classList.add("revealed");
                    io.unobserve(entry.target);
                }
            });
        },
        { threshold: 0.12 }
    );
    revealEls.forEach(function (el) { io.observe(el); });
})();

/* ---- Track Repair widget: digit-only mobile + double-submit guard ---- */
(function () {
    function init() {
        var form = document.getElementById("repairTrackForm");
        if (!form) return;
        var phone = document.getElementById("trackPhone");
        if (phone) {
            phone.addEventListener("input", function () {
                this.value = this.value.replace(/\D/g, "").slice(0, 10);
            });
        }
        form.addEventListener("submit", function () {
            var btn = document.getElementById("trackSubmitBtn");
            if (btn) setTimeout(function () { btn.disabled = true; }, 0);
        });
    }
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
