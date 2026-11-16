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
        // Reveal a bit BEFORE the element reaches the viewport (30% of the
        // viewport height below the fold) and at the first visible pixel.
        // The old 12%-visible threshold kept large sections fully transparent
        // until the user had scrolled well into them, so the page looked
        // blank/white while scrolling.
        { threshold: 0, rootMargin: "0px 0px 30% 0px" }
    );
    revealEls.forEach(function (el) { io.observe(el); });

    // Safety net: never leave content invisible. Any block the observer has
    // not reached within 4s (very fast scrolling, unusual zoom levels) is
    // revealed immediately.
    window.setTimeout(function () {
        document.querySelectorAll(".reveal:not(.revealed)").forEach(function (el) {
            el.classList.add("revealed");
        });
    }, 4000);
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
