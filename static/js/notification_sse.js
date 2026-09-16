// static/js/notification_sse.js
(function() {
    'use strict';

    let eventSource = null;
    let reconnectAttempts = 0;
    const MAX_RECONNECT = 5;

    function connectSSE() {
        const userId = document.querySelector('meta[name="user-id"]')?.getAttribute('content');
        if (!userId) return;

        const streamUrl = `/events/?stream=notif-${userId}`;

        if (eventSource) {
            eventSource.close();
        }

        eventSource = new EventSource(streamUrl);

        // Listen for 'refresh' event – trigger HTMX reload of badge & dropdown
        eventSource.addEventListener('refresh', function(e) {
            // Trigger refresh on badge and dropdown containers
            document.dispatchEvent(new CustomEvent('refresh-notification-ui'));
        });

        // Listen for 'badge' event – update count directly (optional)
        eventSource.addEventListener('badge', function(e) {
            try {
                const data = JSON.parse(e.data);
                updateBadge(data.count);
            } catch (err) {}
        });

        // Listen for 'toast' event – show toast notification
        eventSource.addEventListener('toast', function(e) {
            try {
                const data = JSON.parse(e.data);
                if (typeof window.showToast === 'function') {
                    window.showToast(data.type || 'info', data.message, data.title);
                }
            } catch (err) {}
        });

        eventSource.onerror = function() {
            eventSource.close();
            reconnectAttempts++;
            if (reconnectAttempts <= MAX_RECONNECT) {
                setTimeout(connectSSE, 3000 * reconnectAttempts);
            } else {
                console.warn('SSE failed, falling back to HTMX polling (if any).');
            }
        };

        eventSource.onopen = function() {
            reconnectAttempts = 0;
        };
    }

    function updateBadge(count) {
        const badgeEl = document.getElementById('notificationCount');
        if (badgeEl) {
            badgeEl.textContent = count > 0 ? count : '0';
            badgeEl.classList.toggle('d-none', count === 0);
        }
    }

    // Listen for custom event to refresh UI via HTMX
    document.addEventListener('refresh-notification-ui', function() {
        // Refresh badge container
        const badgeContainer = document.querySelector('#notification-badge-container');
        if (badgeContainer) {
            htmx.trigger(badgeContainer, 'refresh');
        }
        // Refresh dropdown container
        const dropdownContainer = document.querySelector('#notification-dropdown');
        if (dropdownContainer) {
            htmx.trigger(dropdownContainer, 'refresh');
        }
    });

    // Start SSE on page load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', connectSSE);
    } else {
        connectSSE();
    }

    // Cleanup
    window.addEventListener('beforeunload', function() {
        if (eventSource) {
            eventSource.close();
            eventSource = null;
        }
    });
})();