// Built-in visitor analytics: keeps the visit's "last seen" time accurate
// while the visitor lingers on a single page (read → idle → leave).
(function () {
    'use strict';
    function ping() {
        try {
            if (navigator.sendBeacon) {
                navigator.sendBeacon('/analytics/ping/');
            } else if (window.fetch) {
                fetch('/analytics/ping/', { method: 'POST', keepalive: true, credentials: 'same-origin' });
            }
        } catch (e) { /* analytics must never break the page */ }
    }
    setInterval(ping, 30000);
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'hidden') ping();
    });
    window.addEventListener('pagehide', ping);
})();
