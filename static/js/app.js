// static/js/app.js
(function () {
    'use strict';

    // ================================================================
    // PORTAL DETECTION
    // ================================================================
    const hasStaffSidebar = !!document.getElementById('sidebar');
    const hasCustomerSidebar = !!document.getElementById('customer-sidebar');
    const hasPublicThemeToggle = !!document.getElementById('themeTogglePublic');

    // ================================================================
    // COMMON HELPERS
    // ================================================================
    function isMobile() {
        return window.innerWidth <= 768;
    }

    // ================================================================
    // TOAST NOTIFICATIONS
    // ================================================================
    let lastToastMessage = '';
    let toastTimeout = null;
    let toastCounter = 0;

    function showToast(level, message, title) {
        if (lastToastMessage === message) return;
        lastToastMessage = message;
        clearTimeout(toastTimeout);
        toastTimeout = setTimeout(() => { lastToastMessage = ''; }, 1000);

        const toastContainer = document.querySelector('.toast-container');
        if (!toastContainer) return;

        const toastId = 'toast-' + Date.now() + '-' + (++toastCounter);
        const bgClass = {
            'success': 'bg-success text-white',
            'danger': 'bg-danger text-white',
            'error': 'bg-danger text-white',
            'warning': 'bg-warning text-dark',
            'info': 'bg-info text-white',
            'secondary': 'bg-secondary text-white',
            'primary': 'bg-primary text-white',
            'dark': 'bg-dark text-white',
        }[level] || 'bg-secondary text-white';

        const titleHtml = title ? `<strong class="me-2">${title}</strong>` : '';
        const toastHTML = `
        <div id="${toastId}" class="toast align-items-center ${bgClass} border-0 shadow-lg"
             role="alert" aria-live="assertive" aria-atomic="true" data-bs-delay="5000">
            <div class="d-flex align-items-center">
                <div class="toast-body">${titleHtml}${message}</div>
                <button type="button" class="btn-close btn-close-white me-2 m-auto"
                        data-bs-dismiss="toast" aria-label="Close"></button>
            </div>
        </div>`;
        toastContainer.insertAdjacentHTML('beforeend', toastHTML);
        const toastEl = document.getElementById(toastId);
        if (!toastEl) return;
        const toast = new bootstrap.Toast(toastEl, { delay: 5000, autohide: true, animation: true });
        toast.show();
        toastEl.addEventListener('hidden.bs.toast', () => {
            if (toastEl.parentNode) toastEl.remove();
        });
    }

    document.body.addEventListener('showToast', function (evt) {
        if (evt.detail && evt.detail.level && evt.detail.message) {
            showToast(evt.detail.level, evt.detail.message, evt.detail.title || '');
        }
    });
    window.showToast = showToast;


    // ================================================================
    // GLOBAL HTMX ERROR HANDLING
    // ================================================================
    // Fires when server returns 4xx/5xx
    document.body.addEventListener('htmx:responseError', function (evt) {
        const xhr = evt.detail.xhr;
        if (!xhr) return;
        const status = xhr.status;
        let msg = 'Something went wrong. Please try again.';

        if (status === 400) msg = 'Please check the form and try again.';
        else if (status === 401) msg = 'Please log in again to continue.';
        else if (status === 403) msg = 'You do not have permission for this action.';
        else if (status === 404) msg = 'The requested record was not found.';
        else if (status === 429) msg = 'Too many requests. Please wait a moment.';
        else if (status >= 500) msg = 'Server error. Please try again in a moment.';

        // Try to extract error from JSON or HTML response
        try {
            const ct = (xhr.getResponseHeader('Content-Type') || '').toLowerCase();
            if (ct.includes('application/json')) {
                const data = JSON.parse(xhr.responseText);
                if (data.error) msg = data.error;
                else if (data.message) msg = data.message;
            }
        } catch (e) { /* ignore parse errors */ }

        showToast('danger', msg);
        console.error('[HTMX responseError]', status, evt.detail);
    });

    // Fires when network request fails entirely (offline, timeout, DNS)
    // document.body.addEventListener('htmx:sendError', function (evt) {
    //     showToast('danger', 'Network error. Please check your connection and try again.');
    //     console.error('[HTMX sendError]', evt.detail);
    // });

    // Fires when hx-request hits a timeout
    document.body.addEventListener('htmx:timeout', function (evt) {
        showToast('warning', 'Request timed out. Please try again.');
        console.warn('[HTMX timeout]', evt.detail);
    });

    // Fires on any swap — if server returned errors HTML, highlight them
    document.body.addEventListener('htmx:afterSwap', function (evt) {
        const target = evt.detail.target;
        if (!target) return;
        const firstError = target.querySelector('.is-invalid, .invalid-feedback.d-block, .alert-danger');
        if (firstError) {
            firstError.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    });

    // ================================================================
    // HTMX: HANDLE HX-TRIGGER HEADER
    // ================================================================
    document.addEventListener('htmx:afterRequest', function (evt) {
        const xhr = evt.detail.xhr;
        if (!xhr) return;
        const triggerHeader = xhr.getResponseHeader('HX-Trigger');
        if (!triggerHeader) return;
        try {
            let data;
            if (triggerHeader.startsWith('{') || triggerHeader.startsWith('[')) {
                data = JSON.parse(triggerHeader);
            } else {
                data = { event: triggerHeader };
            }
            if (data.showToast) {
                showToast(
                    data.showToast.level || 'info',
                    data.showToast.message || '',
                    data.showToast.title || ''
                );
            }
            if (data.closeModal) document.dispatchEvent(new CustomEvent('closeModal'));
            if (data.redirect) window.location.href = data.redirect;
            if (data.event) document.dispatchEvent(new CustomEvent(data.event, { detail: data.detail || {} }));
            for (const [key, value] of Object.entries(data)) {
                if (key !== 'showToast' && key !== 'closeModal' && key !== 'redirect' && key !== 'event') {
                    document.dispatchEvent(new CustomEvent(key, { detail: value }));
                }
            }
        } catch (e) {
            document.dispatchEvent(new CustomEvent(triggerHeader));
        }
    });

    // ================================================================
    // HTMX: SMOOTH SCROLL AFTER SWAP
    // ================================================================
    document.addEventListener('htmx:afterSwap', function (evt) {
        const target = evt.detail.target;
        if (target) {
            const firstError = target.querySelector('.is-invalid, .invalid-feedback');
            if (firstError) firstError.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    });

    // ================================================================
    // CSRF TOKEN
    // ================================================================
    document.addEventListener('DOMContentLoaded', function () {
        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
        if (csrfToken) {
            document.body.addEventListener('htmx:configRequest', function (evt) {
                evt.detail.headers['X-CSRFToken'] = csrfToken;
            });
        }
    });

    // ================================================================
    // MAIN MODAL HANDLING
    // ================================================================
    const mainModalEl = document.getElementById('mainModal');

    if (mainModalEl) {
        document.body.addEventListener('htmx:afterSwap', function (evt) {
            const target = evt.detail && evt.detail.target;
            if (!target || target.id !== 'mainModalContent') return;
            const html = (target.innerHTML || '').trim();
            if (!html) return;
            let modal = bootstrap.Modal.getInstance(mainModalEl);
            if (!modal) modal = new bootstrap.Modal(mainModalEl, { backdrop: 'static', keyboard: true });
            modal.show();
        });
    }

    document.body.addEventListener('closeModal', function () {
        if (mainModalEl) {
            const modal = bootstrap.Modal.getInstance(mainModalEl);
            if (modal) modal.hide();
        }
    });

    // ================================================================
    // QUICK ADD MODAL
    // ================================================================
    const quickAddModalEl = document.getElementById('quickAddModal');

    if (quickAddModalEl) {
        document.body.addEventListener('htmx:afterSwap', function (evt) {
            if (evt.detail.target.id === 'quickAddModalContent') {
                if (mainModalEl) {
                    const mainModal = bootstrap.Modal.getInstance(mainModalEl);
                    if (mainModal) mainModal.hide();
                }
                setTimeout(function () {
                    let modal = bootstrap.Modal.getInstance(quickAddModalEl);
                    if (!modal) modal = new bootstrap.Modal(quickAddModalEl, { backdrop: 'static', keyboard: true });
                    modal.show();
                    const backdrop = document.querySelector('.modal-backdrop');
                    if (backdrop) backdrop.style.zIndex = '1055';
                }, 50);
            }
        });
    }

    document.body.addEventListener('closeModal', function () {
        if (quickAddModalEl) {
            const modal = bootstrap.Modal.getInstance(quickAddModalEl);
            if (modal) modal.hide();
        }
        if (mainModalEl) mainModalEl.style.display = 'none';
        document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
        document.body.classList.remove('modal-open');
        document.body.style.overflow = '';
    });

    // ================================================================
    // STAFF SIDEBAR
    // ================================================================
    if (hasStaffSidebar) {
        const sidebar = document.getElementById('sidebar');
        const toggleBtn = document.getElementById('sidebarToggle');
        const toggleBtnMain = document.getElementById('sidebarToggleMain');

        // Create mobile backdrop
        let mobileBackdrop = document.getElementById('sidebar-backdrop');
        if (!mobileBackdrop) {
            mobileBackdrop = document.createElement('div');
            mobileBackdrop.id = 'sidebar-backdrop';
            mobileBackdrop.className = 'sidebar-backdrop';
            mobileBackdrop.style.display = 'none';
            document.body.appendChild(mobileBackdrop);
        }

        function toggleSidebarStaff() {
            if (!sidebar) return;
            const icon = document.getElementById('toggle-icon');
            if (isMobile()) {
                sidebar.classList.toggle('show-mobile');
                mobileBackdrop.style.display = sidebar.classList.contains('show-mobile') ? 'block' : 'none';
                localStorage.setItem('mobileSidebarOpen', sidebar.classList.contains('show-mobile'));
                if (icon) {
                    icon.classList.remove('bi-chevron-double-right');
                    icon.classList.add('bi-chevron-double-left');
                }
            } else {
                sidebar.classList.toggle('collapsed');
                localStorage.setItem('sidebarCollapsed', sidebar.classList.contains('collapsed'));
                if (icon) {
                    if (sidebar.classList.contains('collapsed')) {
                        icon.classList.remove('bi-chevron-double-left');
                        icon.classList.add('bi-chevron-double-right');
                    } else {
                        icon.classList.remove('bi-chevron-double-right');
                        icon.classList.add('bi-chevron-double-left');
                    }
                }
            }
        }

        function restoreSidebarStaff() {
            if (!sidebar) return;
            const icon = document.getElementById('toggle-icon');
            if (isMobile()) {
                const mobileOpen = localStorage.getItem('mobileSidebarOpen') === 'true';
                if (mobileOpen) {
                    sidebar.classList.add('show-mobile');
                    mobileBackdrop.style.display = 'block';
                } else {
                    sidebar.classList.remove('show-mobile');
                    mobileBackdrop.style.display = 'none';
                }
                sidebar.classList.remove('collapsed');
                if (icon) {
                    icon.classList.remove('bi-chevron-double-right');
                    icon.classList.add('bi-chevron-double-left');
                }
            } else {
                const isCollapsed = localStorage.getItem('sidebarCollapsed') === 'true';
                if (isCollapsed) sidebar.classList.add('collapsed');
                else sidebar.classList.remove('collapsed');
                sidebar.classList.remove('show-mobile');
                mobileBackdrop.style.display = 'none';
                if (icon) {
                    if (sidebar.classList.contains('collapsed')) {
                        icon.classList.remove('bi-chevron-double-left');
                        icon.classList.add('bi-chevron-double-right');
                    } else {
                        icon.classList.remove('bi-chevron-double-right');
                        icon.classList.add('bi-chevron-double-left');
                    }
                }
            }
        }

        if (toggleBtn) toggleBtn.addEventListener('click', toggleSidebarStaff);
        if (toggleBtnMain) toggleBtnMain.addEventListener('click', toggleSidebarStaff);
        if (mobileBackdrop) {
            mobileBackdrop.addEventListener('click', function() {
                sidebar.classList.remove('show-mobile');
                mobileBackdrop.style.display = 'none';
                localStorage.setItem('mobileSidebarOpen', 'false');
            });
        }
        window.addEventListener('resize', restoreSidebarStaff);
        restoreSidebarStaff();
    }

    // ================================================================
    // CUSTOMER SIDEBAR
    // ================================================================
    if (hasCustomerSidebar) {
        const sidebar = document.getElementById('customer-sidebar');
        const toggleBtn = document.getElementById('sidebarToggleCustomer');
        const mobileToggleBtn = document.getElementById('sidebarToggleMainCustomer');

        // Create mobile backdrop
        let customerBackdrop = document.getElementById('customer-sidebar-backdrop');
        if (!customerBackdrop) {
            customerBackdrop = document.createElement('div');
            customerBackdrop.id = 'customer-sidebar-backdrop';
            customerBackdrop.className = 'sidebar-backdrop';
            customerBackdrop.style.display = 'none';
            document.body.appendChild(customerBackdrop);
        }

        function toggleSidebarCustomer() {
            if (!sidebar) return;
            const icon = document.getElementById('toggle-icon-customer');
            if (isMobile()) {
                sidebar.classList.toggle('show-mobile');
                customerBackdrop.style.display = sidebar.classList.contains('show-mobile') ? 'block' : 'none';
                localStorage.setItem('customerMobileSidebarOpen', sidebar.classList.contains('show-mobile'));
                if (icon) {
                    icon.classList.remove('bi-chevron-double-right');
                    icon.classList.add('bi-chevron-double-left');
                }
            } else {
                sidebar.classList.toggle('collapsed');
                localStorage.setItem('customerSidebarCollapsed', sidebar.classList.contains('collapsed'));
                if (icon) {
                    if (sidebar.classList.contains('collapsed')) {
                        icon.classList.remove('bi-chevron-double-left');
                        icon.classList.add('bi-chevron-double-right');
                    } else {
                        icon.classList.remove('bi-chevron-double-right');
                        icon.classList.add('bi-chevron-double-left');
                    }
                }
            }
        }

        function restoreSidebarCustomer() {
            if (!sidebar) return;
            const icon = document.getElementById('toggle-icon-customer');
            if (isMobile()) {
                const mobileOpen = localStorage.getItem('customerMobileSidebarOpen') === 'true';
                if (mobileOpen) {
                    sidebar.classList.add('show-mobile');
                    customerBackdrop.style.display = 'block';
                } else {
                    sidebar.classList.remove('show-mobile');
                    customerBackdrop.style.display = 'none';
                }
                sidebar.classList.remove('collapsed');
                if (icon) {
                    icon.classList.remove('bi-chevron-double-right');
                    icon.classList.add('bi-chevron-double-left');
                }
            } else {
                const isCollapsed = localStorage.getItem('customerSidebarCollapsed') === 'true';
                if (isCollapsed) sidebar.classList.add('collapsed');
                else sidebar.classList.remove('collapsed');
                sidebar.classList.remove('show-mobile');
                customerBackdrop.style.display = 'none';
                if (icon) {
                    if (sidebar.classList.contains('collapsed')) {
                        icon.classList.remove('bi-chevron-double-left');
                        icon.classList.add('bi-chevron-double-right');
                    } else {
                        icon.classList.remove('bi-chevron-double-right');
                        icon.classList.add('bi-chevron-double-left');
                    }
                }
            }
        }

        function closeCustomerMobile() {
            if (sidebar && isMobile()) {
                sidebar.classList.remove('show-mobile');
                customerBackdrop.style.display = 'none';
                localStorage.setItem('customerMobileSidebarOpen', 'false');
                const icon = document.getElementById('toggle-icon-customer');
                if (icon) {
                    icon.classList.remove('bi-chevron-double-right');
                    icon.classList.add('bi-chevron-double-left');
                }
            }
        }

        if (toggleBtn) toggleBtn.addEventListener('click', toggleSidebarCustomer);
        if (mobileToggleBtn) mobileToggleBtn.addEventListener('click', toggleSidebarCustomer);
        if (sidebar) {
            sidebar.querySelectorAll('.nav-link').forEach(link => link.addEventListener('click', closeCustomerMobile));
        }
        if (customerBackdrop) {
            customerBackdrop.addEventListener('click', closeCustomerMobile);
        }
        const pageWrapper = document.getElementById('page-content-wrapper');
        if (pageWrapper) pageWrapper.addEventListener('click', closeCustomerMobile);
        window.addEventListener('resize', restoreSidebarCustomer);
        restoreSidebarCustomer();
    }

    // ================================================================
    // THEME TOGGLES
    // ================================================================
    if (hasStaffSidebar) {
        const htmlEl = document.documentElement;
        const themeToggle = document.getElementById('themeToggle');
        const themeIcon = document.getElementById('themeIcon');

        function setThemeStaff(theme) {
            htmlEl.setAttribute('data-bs-theme', theme);
            if (themeIcon) {
                if (theme === 'dark') {
                    themeIcon.classList.remove('bi-moon-fill');
                    themeIcon.classList.add('bi-sun-fill');
                } else {
                    themeIcon.classList.remove('bi-sun-fill');
                    themeIcon.classList.add('bi-moon-fill');
                }
            }
            localStorage.setItem('theme', theme);
        }
        function toggleThemeStaff() {
            setThemeStaff(htmlEl.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark');
        }
        if (themeToggle) themeToggle.addEventListener('click', toggleThemeStaff);
        setThemeStaff(localStorage.getItem('theme') || 'light');
    }

    if (hasCustomerSidebar) {
        const htmlEl = document.documentElement;
        const themeToggle = document.getElementById('themeToggleCustomer');
        const themeIcon = document.getElementById('themeIconCustomer');

        function setThemeCustomer(theme) {
            htmlEl.setAttribute('data-bs-theme', theme);
            if (themeIcon) {
                if (theme === 'dark') {
                    themeIcon.classList.remove('bi-moon-fill');
                    themeIcon.classList.add('bi-sun-fill');
                } else {
                    themeIcon.classList.remove('bi-sun-fill');
                    themeIcon.classList.add('bi-moon-fill');
                }
            }
            localStorage.setItem('customerTheme', theme);
        }
        function toggleThemeCustomer() {
            setThemeCustomer(htmlEl.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark');
        }
        if (themeToggle) themeToggle.addEventListener('click', toggleThemeCustomer);
        setThemeCustomer(localStorage.getItem('customerTheme') || 'light');
    }

    if (hasPublicThemeToggle) {
        const htmlEl = document.documentElement;
        const themeToggle = document.getElementById('themeTogglePublic');
        const themeIcon = document.getElementById('themeIconPublic');

        function setThemePublic(theme) {
            htmlEl.setAttribute('data-bs-theme', theme);
            if (themeIcon) {
                if (theme === 'dark') {
                    themeIcon.classList.remove('bi-moon-fill');
                    themeIcon.classList.add('bi-sun-fill');
                } else {
                    themeIcon.classList.remove('bi-sun-fill');
                    themeIcon.classList.add('bi-moon-fill');
                }
            }
            localStorage.setItem('publicTheme', theme);
        }
        function toggleThemePublic() {
            setThemePublic(htmlEl.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark');
        }
        if (themeToggle) themeToggle.addEventListener('click', toggleThemePublic);
        setThemePublic(localStorage.getItem('publicTheme') || 'light');
    }

    // ================================================================
    // KEYBOARD SHORTCUTS
    // ================================================================
    document.addEventListener('keydown', function (e) {
        if (e.ctrlKey && e.shiftKey && (e.key === 's' || e.key === 'S')) {
            e.preventDefault();
            if (hasStaffSidebar) document.getElementById('sidebarToggle')?.click();
            else if (hasCustomerSidebar) document.getElementById('sidebarToggleCustomer')?.click();
        }
        if (e.ctrlKey && e.shiftKey && (e.key === 't' || e.key === 'T')) {
            e.preventDefault();
            if (hasStaffSidebar) document.getElementById('themeToggle')?.click();
            else if (hasCustomerSidebar) document.getElementById('themeToggleCustomer')?.click();
            else if (hasPublicThemeToggle) document.getElementById('themeTogglePublic')?.click();
        }
        if (e.key === 'Escape') {
            const openModal = document.querySelector('.modal.show');
            if (openModal) {
                const modal = bootstrap.Modal.getInstance(openModal);
                if (modal) modal.hide();
            }
        }
    });

    // ================================================================
    // NOTIFICATION COUNT REFRESH
    // ================================================================
    // Fired by mark-read / mark-all-read / delete actions via HTMX
    // `hx-on::after-request`. Instantly updates all badge counters
    // (sidebar + navbar) across both staff and customer portals.
    // ================================================================
    document.body.addEventListener('notificationCountChanged', function () {
        // Refresh all badge wrappers (HTMX will re-fetch count)
        var badgeIds = [
            'sidebar-notif-badge',
            'notificationCount',
            'customer-sidebar-notif-badge',
        ];
        badgeIds.forEach(function (id) {
            var el = document.getElementById(id);
            if (el) htmx.trigger(el, 'load');
        });

        // Refresh the dropdown (list of latest notifications)
        var dropdown = document.getElementById('notification-dropdown');
        if (dropdown) htmx.trigger(dropdown, 'load');
    });

    // ---- Open WhatsApp when an estimate is dispatched via WhatsApp ----
    document.addEventListener('openWhatsApp', function (evt) {
        const url = evt.detail;
        if (!url || typeof url !== 'string') return;
        window.open(url, '_blank', 'noopener,noreferrer');
    });

    // ================================================================
    // GLOBAL SEARCH - Vanilla JS (no HTMX)
    // ================================================================
    const searchWrapper = document.getElementById('global-search-wrapper');
    const searchInput = document.getElementById('global-search-input');
    const searchResults = document.getElementById('global-search-results');
    const searchSpinner = document.getElementById('global-search-spinner');

    if (searchWrapper && searchInput && searchResults) {
        let debounceTimer = null;
        let lastQuery = '';
        let abortController = null;

        function showResults() {
            searchWrapper.classList.remove('search-closed');
        }

        function hideResults() {
            searchWrapper.classList.add('search-closed');
        }

        function doSearch(q) {
            if (abortController) abortController.abort();
            abortController = new AbortController();

            if (searchSpinner) searchSpinner.style.display = 'inline-block';

            fetch('/search/?q=' + encodeURIComponent(q), {
                signal: abortController.signal,
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            })
                .then(function (response) {
                    if (!response.ok) throw new Error('HTTP ' + response.status);
                    return response.text();
                })
                .then(function (html) {
                    searchResults.innerHTML = html;
                    showResults();
                })
                .catch(function (err) {
                    if (err.name === 'AbortError') return;
                    console.error('Search error:', err);
                    searchResults.innerHTML =
                        '<div class="global-search-empty text-danger">Search failed: ' +
                        err.message + '</div>';
                    showResults();
                })
                .finally(function () {
                    if (searchSpinner) searchSpinner.style.display = 'none';
                });
        }

        searchInput.addEventListener('input', function () {
            const q = this.value.trim();
            clearTimeout(debounceTimer);

            if (q.length < 2) {
                searchResults.innerHTML = '';
                hideResults();
                lastQuery = '';
                return;
            }

            if (q === lastQuery) return;

            debounceTimer = setTimeout(function () {
                lastQuery = q;
                doSearch(q);
            }, 300);
        });

        searchInput.addEventListener('focus', function () {
            showResults();
        });

        searchInput.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                hideResults();
                searchInput.blur();
            }
        });

        searchInput.addEventListener('keydown', function (e) {
            const items = searchResults.querySelectorAll('.global-search-item');
            if (!items.length) return;

            const current = searchResults.querySelector('.global-search-item.kb-active');
            let idx = Array.from(items).indexOf(current);

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (current) current.classList.remove('kb-active');
                idx = (idx + 1) % items.length;
                items[idx].classList.add('kb-active');
                items[idx].scrollIntoView({ block: 'nearest' });
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (current) current.classList.remove('kb-active');
                idx = idx <= 0 ? items.length - 1 : idx - 1;
                items[idx].classList.add('kb-active');
                items[idx].scrollIntoView({ block: 'nearest' });
            } else if (e.key === 'Enter') {
                if (current) {
                    e.preventDefault();
                    current.click();
                    hideResults();
                    searchInput.blur();
                }
            }
        });

        document.addEventListener('click', function (e) {
            if (!searchWrapper.contains(e.target)) {
                hideResults();
            }
        });

        document.addEventListener('keydown', function (e) {
            if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
                e.preventDefault();
                searchWrapper.classList.remove('search-closed');
                searchInput.focus();
                searchInput.select();
            }
        });
    }

    // ================================================================
    // SIDEBAR DRAG-RESIZE (Staff + Customer)
    // ================================================================
    function setupSidebarResize(sidebar, opts) {
        if (!sidebar) return;

        let handle = sidebar.querySelector('.sidebar-resize-handle');
        if (!handle) {
            handle = document.createElement('div');
            handle.className = 'sidebar-resize-handle';
            handle.setAttribute('title', 'Drag to resize - Double-click to reset');
            sidebar.appendChild(handle);
        }

        const minWidth = opts.min || 50;
        const maxWidth = opts.max || 420;
        const narrowBreakpoint = 120;
        const storageKey = opts.storageKey;

        function applyWidth(px) {
            const w = Math.max(minWidth, Math.min(maxWidth, Math.round(px)));
            sidebar.style.setProperty('width', w + 'px', 'important');
            sidebar.style.setProperty('min-width', w + 'px', 'important');
            sidebar.style.setProperty('max-width', w + 'px', 'important');
            sidebar.classList.toggle('narrow', w < narrowBreakpoint);
        }

        function clearInlineWidth() {
            sidebar.style.removeProperty('width');
            sidebar.style.removeProperty('min-width');
            sidebar.style.removeProperty('max-width');
            sidebar.classList.remove('narrow');
        }

        if (!isMobile() && !sidebar.classList.contains('collapsed')) {
            const saved = parseInt(localStorage.getItem(storageKey) || '0', 10);
            if (saved >= minWidth && saved <= maxWidth) applyWidth(saved);
        }

        let resizing = false;
        let startX = 0;
        let startW = 0;

        handle.addEventListener('mousedown', function (e) {
            if (isMobile()) return;
            if (sidebar.classList.contains('collapsed')) return;
            e.preventDefault();
            e.stopPropagation();
            resizing = true;
            startX = e.clientX;
            startW = sidebar.getBoundingClientRect().width;
            sidebar.classList.add('resizing');
            handle.classList.add('resizing');
            document.body.classList.add('sidebar-resizing');
        });

        document.addEventListener('mousemove', function (e) {
            if (!resizing) return;
            applyWidth(startW + (e.clientX - startX));
        });

        document.addEventListener('mouseup', function () {
            if (!resizing) return;
            resizing = false;
            sidebar.classList.remove('resizing');
            handle.classList.remove('resizing');
            document.body.classList.remove('sidebar-resizing');

            const w = parseInt(sidebar.style.width, 10);
            if (w >= minWidth && w <= maxWidth) {
                localStorage.setItem(storageKey, w);
            }
        });

        handle.addEventListener('dblclick', function (e) {
            e.preventDefault();
            e.stopPropagation();
            localStorage.removeItem(storageKey);
            clearInlineWidth();
        });

        let lastCollapsedState = sidebar.classList.contains('collapsed');
        const mo = new MutationObserver(function () {
            if (resizing) return;
            const isCollapsedNow = sidebar.classList.contains('collapsed');
            if (isCollapsedNow === lastCollapsedState) return;
            lastCollapsedState = isCollapsedNow;

            if (isCollapsedNow) {
                const w = parseInt(sidebar.style.width, 10);
                if (w && w >= minWidth && w <= maxWidth) {
                    localStorage.setItem(storageKey, w);
                }
                clearInlineWidth();
            } else if (!isMobile()) {
                const saved = parseInt(localStorage.getItem(storageKey) || '0', 10);
                if (saved >= minWidth && saved <= maxWidth) applyWidth(saved);
            } else {
                clearInlineWidth();
            }
        });
        mo.observe(sidebar, { attributes: true, attributeFilter: ['class'] });

        window.addEventListener('resize', function () {
            if (isMobile()) clearInlineWidth();
        });
    }

    if (hasStaffSidebar) {
        setupSidebarResize(document.getElementById('sidebar'), {
            min: 50,
            max: 420,
            storageKey: 'staffSidebarWidth'
        });
    }

    if (hasCustomerSidebar) {
        setupSidebarResize(document.getElementById('customer-sidebar'), {
            min: 50,
            max: 420,
            storageKey: 'customerSidebarWidth'
        });
    }

})();