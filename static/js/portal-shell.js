/* static/js/portal-shell.js — staff/customer app shell behaviors */
(function () {
    "use strict";

    var topbar = document.querySelector(".app-topbar");
    if (topbar) {
        var onScroll = function () {
            topbar.classList.toggle("is-scrolled", window.scrollY > 8);
        };
        window.addEventListener("scroll", onScroll, { passive: true });
        onScroll();
    }
})();
