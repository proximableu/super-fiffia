/* Shared F&S WebUI behaviour (WEBUI.md §1 + §5). Loaded once, deferred.
 *
 * Owns the two pieces of behaviour that live on the shared base layout:
 *   1. the top navigation — marks the active stage link so /ingest and /chat
 *      show which stage you are on (§1);
 *   2. the language selector (§5): posts to `POST /lang`, persists the choice
 *      in a cookie, toggles the active SV/EN button, and dispatches a
 *      `languagechange` CustomEvent (lang = the new value) so each stage page
 *      can re-render its own labels.
 */
(function () {
    "use strict";

    var LANG_COOKIE = "lang";

    function getCookie(name) {
        var match = document.cookie.match(
            new RegExp("(?:^|; )" + name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "=([^;]*)")
        );
        return match ? decodeURIComponent(match[1]) : "";
    }

    function setCookie(name, value) {
        var expires = new Date(Date.now() + 60 * 60 * 24 * 365 * 1000).toUTCString();
        document.cookie = name + "=" + encodeURIComponent(value) +
            "; expires=" + expires + "; path=/; SameSite=Lax";
    }

    function setNavActive(page) {
        document.querySelectorAll(".nav-link").forEach(function (link) {
            var active = link.getAttribute("data-nav") === page;
            link.classList.toggle("active", active);
        });
    }

    function setLangButtonsActive(lang) {
        document.querySelectorAll(".lang-toggle button").forEach(function (btn) {
            btn.classList.toggle("active", btn.getAttribute("data-lang") === lang);
        });
    }

    function notifyLanguageChange(lang) {
        var event = document.createEvent("CustomEvent");
        event.initCustomEvent("languagechange", true, true, { lang: lang });
        document.dispatchEvent(event);
    }

    function init() {
        var lang = getCookie(LANG_COOKIE) || "sv";
        document.documentElement.lang = lang;
        setLangButtonsActive(lang);
        setNavActive(location.pathname === "/chat" ? "chat" : "ingest");

        // Language selector (§5): post to the server, keep the local button in
        // sync, and let each stage page re-render its own labels.
        document.querySelectorAll(".lang-toggle button").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var next = this.getAttribute("data-lang");
                setCookie(LANG_COOKIE, next);
                setLangButtonsActive(next);
                fetch("/lang", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ lang: next })
                }).catch(function () { /* keep local state even if the server misses */ });
                notifyLanguageChange(next);
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
