    def evaluate_js(self, script: str, page: "Page | None" = None) -> any:
        """
        Evaluates a JavaScript expression on the given page (or self._page).

        Useful for operations that have no dedicated CSS-selector primitive, e.g.:
          - Pausing a <video> element:  "document.querySelector('video')?.pause()"
          - Checking player state:      "document.querySelector('video')?.paused"
          - Scrolling to a position:    "window.scrollTo(0, 500)"

        Phase 9: Used by YouTubeSkill.open_top_results to pause each new tab's
        video after it loads, so multiple tabs don't all play simultaneously.

        Stable Contract (Phase 9):
          evaluate_js(script: str, page=None) → any

        Args:
            script: JavaScript expression or arrow-function string to evaluate.
                    Must be a valid JS expression, not a statement block.
                    Example: "document.querySelector('video')?.pause()"
            page:   Optional explicit Playwright Page to run the script on.
                    If None → runs on self._page (the Actions-instance's own page).
                    Pass a `new_page` here when operating on a tab that was opened
                    via open_new_tab() and wrapped in its own Actions instance.

        Returns:
            The JavaScript return value (None for void expressions like pause()).

        Raises:
            ActionError: if the JS evaluation throws an exception.
        """
        target = page if page is not None else self._page
        logger.debug(f"[evaluate_js] Script: {script[:120]!r}")
        try:
            result = target.evaluate(script)
            logger.debug(f"[evaluate_js] ✓ Ergebnis: {result!r}")
            return result
        except Exception as exc:
            raise ActionError(
                f"[evaluate_js] JS-Ausführung fehlgeschlagen: "
                f"{type(exc).__name__}: {exc} | Script: {script[:80]!r}"
            ) from exc
