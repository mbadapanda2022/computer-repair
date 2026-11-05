/*
 * IC Finder behaviour. The maths lives on the server (accounting/ic_catalog.py)
 * because this tool reads a catalog, not a formula — so unlike the calculators
 * this module is mostly about keeping the technician's place: the query stays
 * in the box, the pin rows he typed stay filled, and opening a pinout never
 * throws away the list he was comparing.
 */
(function () {
    'use strict';

    var SEARCH_DEBOUNCE = 250;   /* ms — long enough to skip a keystroke burst,
                                    short enough that the list feels live */

    TT.tools['ic-finder'] = function (root) {
        var query = TT.q(root, 'icQuery');
        var searchForm = root.querySelector('.ic-search-form');
        var pinForm = TT.q(root, 'icPinForm');
        var pinRows = TT.q(root, 'icPinRows');
        var addRow = TT.q(root, 'icAddRow');

        /* The rows the page shipped are what Clear goes back to — a row he added
         * with "+ pin row" is his scratch, not part of the form he reset. */
        var servedRows = pinRows ? pinRows.innerHTML : '';

        /* ── tabs: two jobs, one page, and neither loses what the other typed ── */
        function showTab(name) {
            root.querySelectorAll('[data-ictab]').forEach(function (btn) {
                var on = btn.dataset.ictab === name;
                btn.classList.toggle('active', on);
                btn.setAttribute('aria-selected', on ? 'true' : 'false');
            });
            ['search', 'pins'].forEach(function (panel) {
                var box = root.querySelector('#ic' + (panel === 'search' ? 'Search' : 'Pin') + 'Panel');
                if (box) box.hidden = panel !== name;
            });
        }

        root.querySelectorAll('[data-ictab]').forEach(function (btn) {
            btn.addEventListener('click', function () { showTab(btn.dataset.ictab); });
        });

        /* The empty states hand him the other mode — one tap, no guessing. */
        root.addEventListener('click', function (evt) {
            var jump = evt.target.closest('[data-ic-goto-tab]');
            if (!jump || !root.contains(jump)) return;
            showTab(jump.dataset.icGotoTab);
            var focus = jump.dataset.icGotoTab === 'search' ? query
                : pinRows.querySelector('input[name="pin"]');
            if (focus) focus.focus();
        });

        /* Clear resets the boxes; the two panels then ask the server for their
         * "nothing typed yet" state, so no area is ever left blank. */
        root.addEventListener('click', function (evt) {
            if (!evt.target.closest('[data-clear]')) return;
            root.querySelectorAll('.ic-pinout-slot').forEach(function (slot) {
                slot.innerHTML = '';
            });
            root.querySelectorAll('[data-ic-expand]').forEach(function (btn) {
                btn.setAttribute('aria-expanded', 'false');
            });
            setTimeout(function () {
                if (pinRows) pinRows.innerHTML = servedRows;
                runSearch();
                if (pinForm && window.htmx) htmx.trigger(pinForm, 'submit');
            }, 0);
        });

        /* ── job A: live search on the marking code or part number ── */
        var timer = null;
        function runSearch() {
            if (!searchForm || !window.htmx) return;
            htmx.trigger(searchForm, 'ic-search');
        }

        if (query) {
            query.addEventListener('input', function () {
                clearTimeout(timer);
                timer = setTimeout(runSearch, SEARCH_DEBOUNCE);
            });
            /* Pressing Escape on a search box should empty it, not close the tab. */
            query.addEventListener('keydown', function (evt) {
                if (evt.key === 'Escape') {
                    query.value = '';
                    clearTimeout(timer);
                    runSearch();
                }
            });
        }

        /* Popular-chip chips: fill the box and search immediately. */
        root.addEventListener('click', function (evt) {
            var pick = evt.target.closest('[data-ic-pick]');
            if (!pick || !root.contains(pick) || !query) return;
            query.value = pick.dataset.icPick;
            clearTimeout(timer);
            runSearch();
            query.focus();
        });

        /* ── job B: pin constraint rows ── */
        function lastRow() {
            return pinRows ? pinRows.querySelector('.ic-row:last-of-type') : null;
        }

        function addPinRow() {
            var row = lastRow();
            if (!row || !pinRows) return;
            var fresh = row.cloneNode(true);
            fresh.querySelectorAll('input').forEach(function (box) { box.value = ''; });
            fresh.querySelectorAll('select').forEach(function (picker) { picker.value = ''; });
            pinRows.appendChild(fresh);
            fresh.querySelector('input').focus();
        }

        if (addRow) addRow.addEventListener('click', addPinRow);

        root.addEventListener('click', function (evt) {
            var kill = evt.target.closest('.ic-row-remove');
            if (!kill || !root.contains(kill)) return;
            if (pinRows.querySelectorAll('.ic-row').length <= 1) {
                /* Never leave the form with nothing to type into. */
                pinRows.querySelectorAll('input, select').forEach(function (field) {
                    field.value = '';
                });
                return;
            }
            kill.closest('.ic-row').remove();
        });

        /* ── inline pinout: open several, compare, close one without a reload ── */
        root.addEventListener('click', function (evt) {
            var btn = evt.target.closest('[data-ic-expand]');
            if (!btn || !root.contains(btn)) return;
            var slot = root.querySelector('#icPinout-' + btn.dataset.icExpand);
            if (!slot || !slot.innerHTML.trim()) return;
            /* Capture phase: a loaded pinout collapses here, and stopPropagation
             * keeps htmx's own click handler on the same button from firing a
             * request whose only purpose would be to fetch what is already on
             * screen. */
            evt.stopPropagation();
            evt.preventDefault();
            slot.innerHTML = '';
            btn.setAttribute('aria-expanded', 'false');
        }, true);

        root.addEventListener('click', function (evt) {
            var btn = evt.target.closest('[data-ic-expand]');
            if (!btn || !root.contains(btn)) return;
            var slot = root.querySelector('#icPinout-' + btn.dataset.icExpand);
            if (slot) slot.dataset.icSlot = btn.dataset.icExpand;
            btn.setAttribute('aria-expanded', 'true');
        });

        /* Results and pinouts both arrive by HTMX, so the hover links are bound
         * after every swap — an unbound diagram is pins that do nothing when
         * he taps them. htmx bubbles afterSwap from the swapped node, so the
         * tool root hears its own swaps and nothing else's; a document listener
         * here would survive the pane it belongs to and stack up on every
         * history restore. */
        root.addEventListener('htmx:afterSwap', function (evt) {
            var target = evt.detail && evt.detail.target;
            if (!target || !root.contains(target)) return;
            syncHover(target);
            if (target.classList.contains('ic-pinout-slot')) {
                var btn = root.querySelector(
                    '[data-ic-expand="' + target.dataset.icSlot + '"]');
                if (btn) btn.setAttribute('aria-expanded', 'true');
            }
        });

        /* ── pin diagram <-> pin table, both directions ── */
        function markPins(scope, number, on) {
            scope.querySelectorAll('[data-pin="' + number + '"]').forEach(function (node) {
                node.classList.toggle('ic-hot', on);
            });
        }

        function bindHover(node) {
            var scope = node.closest('.ic-pinout') || root;
            var number = node.dataset.pin;
            if (!number) return;
            ['mouseenter', 'focus'].forEach(function (type) {
                node.addEventListener(type, function () { markPins(scope, number, true); });
            });
            ['mouseleave', 'blur'].forEach(function (type) {
                node.addEventListener(type, function () { markPins(scope, number, false); });
            });
        }

        function syncHover(scope) {
            scope.querySelectorAll('[data-pin]').forEach(bindHover);
        }

        /* ── copy and print, because the bench wall still runs on paper ── */
        function flash(btn, text) {
            var original = btn.innerHTML;
            btn.innerHTML = text;
            btn.disabled = true;
            setTimeout(function () {
                btn.innerHTML = original;
                btn.disabled = false;
            }, 1400);
        }

        root.addEventListener('click', function (evt) {
            var copy = evt.target.closest('[data-ic-copy]');
            if (copy && root.contains(copy)) {
                var source = copy.closest('.ic-pinout').querySelector('.ic-copy-source');
                var text = source ? source.value : '';
                if (!text) return;
                var done = function () { flash(copy, '<i class="bi bi-check-lg"></i> Copied'); };
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text).then(done, function () {
                        selectAndCopy(source, done);
                    });
                } else {
                    selectAndCopy(source, done);
                }
                return;
            }

            var print = evt.target.closest('[data-ic-print]');
            if (print && root.contains(print)) {
                var card = print.closest('.ic-card') || print.closest('.ic-pinout');
                if (!card) return;
                card.classList.add('ic-printing');
                var finish = function () {
                    card.classList.remove('ic-printing');
                    window.removeEventListener('afterprint', finish);
                };
                window.addEventListener('afterprint', finish);
                window.print();
                /* Some browsers never fire afterprint on a cancelled dialog. */
                setTimeout(finish, 4000);
            }
        });

        function selectAndCopy(field, done) {
            field.select();
            field.setSelectionRange(0, field.value.length);
            try {
                document.execCommand('copy');
                done();
            } catch (err) {
                /* Clipboard is blocked — the text is selected, so he can copy
                 * it himself rather than watch a silent failure. */
            }
        }

        /* A pane coming back from htmx's history cache keeps its markup; make
         * the visible tab agree with it. */
        showTab(root.querySelector('[data-ictab].active')
            ? root.querySelector('[data-ictab].active').dataset.ictab : 'search');
    };
})();
