/*
 * Tech Tools page shell behaviour: mobile sidebar, sidebar search, active
 * state + document title on HTMX navigation, and booting the tool module that
 * owns the markup currently in the pane.
 *
 * Tool code itself lives in static/js/tools/<group>.js — each tool registers
 * TT.tools[slug] = initFunction(root) and is initialised here, so the same
 * path runs for a server-rendered page load and for an HTMX swap.
 */
(function () {
    /* Sidebar links all live in the shell (static/js/tech-tools.js is loaded
     * deferred, after the body is parsed), so a plain getElementById finds the
     * element that carries the full per-tool SEO title. */
    var sidebar = document.getElementById('ttSidebar');
    var toggleBtn = document.getElementById('ttSidebarToggle');
    var closeBtn = document.getElementById('ttSidebarClose');
    var searchBox = document.getElementById('ttSearch');
    var emptyNote = document.getElementById('ttSearchEmpty');

    function setSidebar(open) {
        if (!sidebar) return;
        sidebar.classList.toggle('open', open);
        if (toggleBtn) toggleBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    if (toggleBtn && sidebar) {
        toggleBtn.addEventListener('click', function () {
            setSidebar(!sidebar.classList.contains('open'));
        });
    }
    if (closeBtn) closeBtn.addEventListener('click', function () { setSidebar(false); });
    document.addEventListener('keydown', function (evt) {
        if (evt.key === 'Escape') setSidebar(false);
    });

    /* Sidebar filter — hides links and whole groups that do not match. */
    if (searchBox) {
        searchBox.addEventListener('input', function () {
            var q = searchBox.value.trim().toLowerCase();
            var visible = 0;
            document.querySelectorAll('.tt-nav-group').forEach(function (group) {
                var shown = 0;
                group.querySelectorAll('.tt-nav-link').forEach(function (link) {
                    var text = link.textContent.toLowerCase();
                    var hit = !q || text.indexOf(q) !== -1;
                    link.classList.toggle('tt-hidden', !hit);
                    if (hit) shown++;
                });
                group.classList.toggle('tt-hidden', shown === 0);
                visible += shown;
            });
            if (emptyNote) emptyNote.hidden = visible !== 0;
        });
    }

    function markActive(link) {
        document.querySelectorAll('.tt-nav-link').forEach(function (el) {
            el.classList.remove('active');
            el.removeAttribute('aria-current');
        });
        if (link) {
            link.classList.add('active');
            link.setAttribute('aria-current', 'page');
        }
    }

    /* The slug of the tool currently inside a pane ('' for the welcome pane). */
    function paneSlug(target) {
        var pane = target ? target.querySelector('.tt-tool[data-tool]') : null;
        return pane ? pane.dataset.tool : '';
    }

    function navLink(slug) {
        if (!slug) return null;
        return document.querySelector('.tt-nav-link[data-slug="' + slug + '"]');
    }

    /* The pane's chrome — which sidebar row is lit, and what the tab says. The
     * nav link is the only place carrying a tool's full title, and the shell
     * hands over the welcome title because after a Back to /tools/ there is no
     * tool left to look one up for. */
    function syncChrome(target) {
        var link = navLink(paneSlug(target));
        markActive(link);
        if (link && link.dataset.title) {
            document.title = link.dataset.title;
        } else if (target && target.dataset.indexTitle) {
            document.title = target.dataset.indexTitle;
        }
    }

    /* Boot the tool inside a pane, on the initial render and after every swap.
     *
     * Swapped markup is brand-new elements, and a restore from htmx's history
     * cache is no different — except that the cached HTML keeps the
     * data-tt-inited marker (which would make TT.boot skip the element, leaving
     * a panel bound to nothing) and the mode button the user had last clicked
     * (which would disagree with the tool's own fresh state). So the marker is
     * dropped before booting, and the button the page ships as active is
     * clicked again once its handler exists. */
    function bootPane(target) {
        if (!target || !window.TT || typeof TT.boot !== 'function') return;
        target.querySelectorAll('.tt-tool[data-tool]').forEach(function (el) {
            delete el.dataset[TT.INITED];
        });
        TT.boot(target);
        target.querySelectorAll('.tt-tool [data-default-mode]').forEach(function (btn) {
            btn.click();
        });
        syncChrome(target);
    }

    /* Welcome pane's "Open a tool" select: drive the sidebar link so HTMX
     * swaps the pane and pushes the URL exactly like a click would. */
    document.addEventListener('change', function (evt) {
        var pick = evt.target;
        if (!pick || pick.id !== 'ttQuickPick' || !pick.value) return;
        var link = navLink(pick.options[pick.selectedIndex].dataset.slug);
        if (link) {
            link.click();
        } else {
            window.location.href = pick.value;
        }
    });

    document.addEventListener('DOMContentLoaded', function () {
        bootPane(document.getElementById('ttContent'));
    });

    document.body.addEventListener('htmx:beforeRequest', function (evt) {
        var elt = evt.detail.elt;
        if (elt && elt.classList && elt.classList.contains('tt-nav-link')) {
            markActive(elt);
            if (window.innerWidth < 992) setSidebar(false);
        }
    });

    document.body.addEventListener('htmx:afterSwap', function (evt) {
        if (evt.detail.target && evt.detail.target.id === 'ttContent') {
            bootPane(evt.detail.target);
        }
    });

    document.body.addEventListener('htmx:responseError', function (evt) {
        if (evt.detail.target && evt.detail.target.id === 'ttContent') {
            evt.detail.target.innerHTML =
                '<div class="alert alert-danger my-3" role="alert">' +
                'Could not load this tool. Please try again.</div>';
        }
    });

    /* Back / Forward after hx-push-url. htmx puts the cached pane markup back
     * itself and then fires htmx:historyRestore — it does NOT fire htmx:afterSwap
     * for a restore, so this is the only place that gets to re-boot the pane.
     * The cached markup still carries data-tt-inited and its last-rendered
     * results, so without a re-boot the tool comes back dead: it shows numbers
     * for fields that no longer hold those values and ignores every keystroke. */
    document.addEventListener('htmx:historyRestore', function () {
        bootPane(document.getElementById('ttContent'));
    });

    /* A cached entry that never existed (an old session, or a bookmark hop) makes
     * htmx fetch the URL and swap it normally, which htmx:afterSwap already
     * handles; this only covers the plain popstate that htmx leaves alone. */
    window.addEventListener('popstate', function () {
        syncChrome(document.getElementById('ttContent'));
    });
})();
