//static/js/app.js
(function() {
    'use strict';

    // ==============================
    // PORTAL DETECTION
    // ==============================
    const hasStaffSidebar = !!document.getElementById('sidebar');
    const hasCustomerSidebar = !!document.getElementById('customer-sidebar');
    const hasPublicThemeToggle = !!document.getElementById('themeTogglePublic');

    // ==============================
    // COMMON HELPERS
    // ==============================
    function isMobile() {
        return window.innerWidth <= 768;
    }

    // ==============================
    // TOAST NOTIFICATIONS
    // ==============================
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
        toastEl.addEventListener('hidden.bs.toast', () => { if (toastEl.parentNode) toastEl.remove(); });
    }

    document.body.addEventListener('showToast', function(evt) {
        if (evt.detail && evt.detail.level && evt.detail.message) {
            showToast(evt.detail.level, evt.detail.message, evt.detail.title || '');
        }
    });
    window.showToast = showToast;

    // ==============================
    // HTMX: HANDLE HX-Trigger HEADER
    // ==============================
    document.addEventListener('htmx:afterRequest', function(evt) {
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
                showToast(data.showToast.level || 'info', data.showToast.message || '', data.showToast.title || '');
            }
            if (data.closeModal) document.dispatchEvent(new CustomEvent('closeModal'));
            if (data.redirect) window.location.href = data.redirect;
            if (data.event) document.dispatchEvent(new CustomEvent(data.event, { detail: data.detail || {} }));
            for (const [key, value] of Object.entries(data)) {
                if (key !== 'showToast' && key !== 'closeModal' && key !== 'redirect' && key !== 'event') {
                    document.dispatchEvent(new CustomEvent(key, { detail: value }));
                }
            }
        } catch(e) {
            document.dispatchEvent(new CustomEvent(triggerHeader));
        }
    });

    // ==============================
    // HTMX: SMOOTH SCROLL AFTER SWAP
    // ==============================
    document.addEventListener('htmx:afterSwap', function(evt) {
        const target = evt.detail.target;
        if (target) {
            const firstError = target.querySelector('.is-invalid, .invalid-feedback');
            if (firstError) firstError.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    });

    // ==============================
    // CSRF TOKEN
    // ==============================
    document.addEventListener('DOMContentLoaded', function() {
        const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
        if (csrfToken) {
            document.body.addEventListener('htmx:configRequest', function(evt) {
                evt.detail.headers['X-CSRFToken'] = csrfToken;
            });
        }
    });

    // ==============================
    // MAIN MODAL HANDLING
    // ==============================
    const mainModalEl = document.getElementById('mainModal');

    if (mainModalEl) {
        document.body.addEventListener('htmx:afterSwap', function(evt) {
            const target = evt.detail && evt.detail.target;
            if (!target || target.id !== 'mainModalContent') return;
            const html = (target.innerHTML || '').trim();
            if (!html) return;
            let modal = bootstrap.Modal.getInstance(mainModalEl);
            if (!modal) modal = new bootstrap.Modal(mainModalEl, { backdrop: 'static', keyboard: true });
            modal.show();
        });
    }

    document.body.addEventListener('closeModal', function() {
        if (mainModalEl) {
            const modal = bootstrap.Modal.getInstance(mainModalEl);
            if (modal) modal.hide();
        }
    });

    // ==============================
    // QUICK ADD MODAL
    // ==============================
    const quickAddModalEl = document.getElementById('quickAddModal');

    if (quickAddModalEl) {
        document.body.addEventListener('htmx:afterSwap', function(evt) {
            if (evt.detail.target.id === 'quickAddModalContent') {
                if (mainModalEl) {
                    const mainModal = bootstrap.Modal.getInstance(mainModalEl);
                    if (mainModal) mainModal.hide();
                }
                setTimeout(function() {
                    let modal = bootstrap.Modal.getInstance(quickAddModalEl);
                    if (!modal) modal = new bootstrap.Modal(quickAddModalEl, { backdrop: 'static', keyboard: true });
                    modal.show();
                    const backdrop = document.querySelector('.modal-backdrop');
                    if (backdrop) backdrop.style.zIndex = '1055';
                }, 50);
            }
        });
    }

    document.body.addEventListener('closeModal', function() {
        if (quickAddModalEl) {
            const modal = bootstrap.Modal.getInstance(quickAddModalEl);
            if (modal) modal.hide();
        }
        if (mainModalEl) mainModalEl.style.display = 'none';
        document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
        document.body.classList.remove('modal-open');
        document.body.style.overflow = '';
    });

    // ==============================
    // STAFF SIDEBAR
    // ==============================
    if (hasStaffSidebar) {
        const sidebar = document.getElementById('sidebar');
        const toggleBtn = document.getElementById('sidebarToggle');
        const toggleBtnMain = document.getElementById('sidebarToggleMain');

        function toggleSidebarStaff() {
            if (!sidebar) return;
            const icon = document.getElementById('toggle-icon');
            if (isMobile()) {
                sidebar.classList.toggle('show-mobile');
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
                if (mobileOpen) sidebar.classList.add('show-mobile');
                else sidebar.classList.remove('show-mobile');
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
        window.addEventListener('resize', restoreSidebarStaff);
        restoreSidebarStaff();
    }

    // ==============================
    // CUSTOMER SIDEBAR
    // ==============================
    if (hasCustomerSidebar) {
        const sidebar = document.getElementById('customer-sidebar');
        const toggleBtn = document.getElementById('sidebarToggleCustomer');
        const mobileToggleBtn = document.getElementById('sidebarToggleMainCustomer');

        function toggleSidebarCustomer() {
            if (!sidebar) return;
            const icon = document.getElementById('toggle-icon-customer');
            if (isMobile()) {
                sidebar.classList.toggle('show-mobile');
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
                if (mobileOpen) sidebar.classList.add('show-mobile');
                else sidebar.classList.remove('show-mobile');
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
        if (sidebar) sidebar.querySelectorAll('.nav-link').forEach(link => link.addEventListener('click', closeCustomerMobile));
        const pageWrapper = document.getElementById('page-content-wrapper');
        if (pageWrapper) pageWrapper.addEventListener('click', closeCustomerMobile);
        window.addEventListener('resize', restoreSidebarCustomer);
        restoreSidebarCustomer();
    }

    // ==============================
    // THEME TOGGLES
    // ==============================
    if (hasStaffSidebar) {
        const htmlEl = document.documentElement;
        const themeToggle = document.getElementById('themeToggle');
        const themeIcon = document.getElementById('themeIcon');
        function setThemeStaff(theme) {
            htmlEl.setAttribute('data-bs-theme', theme);
            if (themeIcon) {
                if (theme === 'dark') { themeIcon.classList.remove('bi-moon-fill'); themeIcon.classList.add('bi-sun-fill'); }
                else { themeIcon.classList.remove('bi-sun-fill'); themeIcon.classList.add('bi-moon-fill'); }
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
                if (theme === 'dark') { themeIcon.classList.remove('bi-moon-fill'); themeIcon.classList.add('bi-sun-fill'); }
                else { themeIcon.classList.remove('bi-sun-fill'); themeIcon.classList.add('bi-moon-fill'); }
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
                if (theme === 'dark') { themeIcon.classList.remove('bi-moon-fill'); themeIcon.classList.add('bi-sun-fill'); }
                else { themeIcon.classList.remove('bi-sun-fill'); themeIcon.classList.add('bi-moon-fill'); }
            }
            localStorage.setItem('publicTheme', theme);
        }
        function toggleThemePublic() {
            setThemePublic(htmlEl.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark');
        }
        if (themeToggle) themeToggle.addEventListener('click', toggleThemePublic);
        setThemePublic(localStorage.getItem('publicTheme') || 'light');
    }

    // ==============================
    // KEYBOARD SHORTCUTS
    // ==============================
    document.addEventListener('keydown', function(e) {
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

    document.addEventListener('refresh-notifications', function() {
        const badgeContainer = document.querySelector('#notification-badge-container');
        if (badgeContainer) htmx.trigger(badgeContainer, 'refresh');
        const dropdownContainer = document.querySelector('#notification-dropdown');
        if (dropdownContainer) htmx.trigger(dropdownContainer, 'refresh');
        const listContainer = document.querySelector('#notification-list-container');
        if (listContainer) htmx.trigger(listContainer, 'refresh');
    });

    // ==============================
    // GLOBAL SEARCH — Vanilla JS (no HTMX)
    // ==============================
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
            // Cancel previous in-flight request
            if (abortController) abortController.abort();
            abortController = new AbortController();

            if (searchSpinner) searchSpinner.style.display = 'inline-block';

            fetch('/search/?q=' + encodeURIComponent(q), {
                signal: abortController.signal,
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            })
            .then(function(response) {
                if (!response.ok) throw new Error('HTTP ' + response.status);
                return response.text();
            })
            .then(function(html) {
                searchResults.innerHTML = html;
                showResults();
            })
            .catch(function(err) {
                if (err.name === 'AbortError') return;
                console.error('Search error:', err);
                searchResults.innerHTML =
                    '<div class="global-search-empty text-danger">Search failed: ' +
                    err.message + '</div>';
                showResults();
            })
            .finally(function() {
                if (searchSpinner) searchSpinner.style.display = 'none';
            });
        }

        // Input — debounced 300ms
        searchInput.addEventListener('input', function() {
            const q = this.value.trim();
            clearTimeout(debounceTimer);

            if (q.length < 2) {
                searchResults.innerHTML = '';
                hideResults();
                lastQuery = '';
                return;
            }

            if (q === lastQuery) return;

            debounceTimer = setTimeout(function() {
                lastQuery = q;
                doSearch(q);
            }, 300);
        });

        // Focus: reveal if we have any content
        searchInput.addEventListener('focus', function() {
            showResults();
        });

        // Esc: hide
        searchInput.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                hideResults();
                searchInput.blur();
            }
        });

        // Keyboard nav in results
        searchInput.addEventListener('keydown', function(e) {
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

        // Outside click: hide
        document.addEventListener('click', function(e) {
            if (!searchWrapper.contains(e.target)) {
                hideResults();
            }
        });

        // Ctrl+K / Cmd+K
        document.addEventListener('keydown', function(e) {
            if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
                e.preventDefault();
                searchWrapper.classList.remove('search-closed');
                searchInput.focus();
                searchInput.select();
            }
        });
    }

})();