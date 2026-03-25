"""
config.py — Zentrale Konfiguration für das Browser Control System.

Alle Module importieren von hier. Nichts ist hardcodiert.
Phase 1: Chrome-Verbindung, Timeouts, Retry-Logik.
Phase 7c: Execution Mode (FAST / HUMAN / AUTO).
"""

# ─── Chrome CDP-Verbindung ─────────────────────────────────────────────────────
# Chrome muss mit --remote-debugging-port=9222 gestartet sein.
# Beispiel: chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\tmp\chrome_debug
CHROME_DEBUG_HOST: str = "localhost"
CHROME_DEBUG_PORT: int = 9222
CHROME_CDP_URL: str = f"http://{CHROME_DEBUG_HOST}:{CHROME_DEBUG_PORT}"

# ─── Timeouts (Sekunden) ──────────────────────────────────────────────────────
# Wie lange wait_for() maximal wartet, bevor es aufgibt.
DEFAULT_TIMEOUT: float = 10.0

# Wie lange zwischen Retry-Versuchen gewartet wird (Sekunden).
# Reduced from 0.5 → 0.05: after a PlaywrightTimeoutError Playwright already
# waited DEFAULT_TIMEOUT (10s); sleeping an extra 500ms is pure waste.
RETRY_DELAY: float = 0.05

# ─── Retry-Logik ─────────────────────────────────────────────────────────────
# Wie oft eine Aktion bei transientem Fehler wiederholt wird.
# Gilt pro Selector-Versuch, nicht pro gesamte Selector-Liste.
MAX_RETRIES: int = 3

# ─── Logging ──────────────────────────────────────────────────────────────────
# "DEBUG"   → alles (jeder Selector-Versuch, jede Retry-Runde)
# "INFO"    → Erfolge + Fehler
# "WARNING" → nur Fehler
LOG_LEVEL: str = "DEBUG"

# ─── Planner (Phase 5 — hier schon definiert, damit config vollständig ist) ───
PLANNER_ENGINE: str = "template"  # "template" | "llm"

# ─── Safety Guards ───────────────────────────────────────────────────────────────
# Disables buy_now() globally. Must be explicitly set to True to allow checkout.
BUY_NOW_ENABLED: bool = False

# ─── Scroll ───────────────────────────────────────────────────────────────────
# Standard-Scrollbetrag in Pixeln, wenn kein amount übergeben wird.
DEFAULT_SCROLL_AMOUNT: int = 500

# ─── Execution Mode (Phase 7c) ────────────────────────────────────────────────
# Steuert wie Aktionen ausgeführt werden.
#
#   "fast"   → Direktausführung, kein Mausbewegung, minimale Wartezeiten.
#              Ideal für: YouTube, Google, Wikipedia.
#
#   "human"  → Simuliert menschliches Verhalten: Mausbewegung, zufällige
#              Verzögerungen (20–80ms), Scroll-ins-View, Stabilitätsprüfung.
#              Ideal für: Amazon, Login-Seiten, Checkouts, Formulare.
#
#   "auto"   → Automatische Erkennung anhand der URL (bekannte Muster)
#              mit dynamischem Fallback über DOM-Analyse.
#
EXECUTION_MODE: str = "auto"  # "fast" | "human" | "auto"

# Verzögerungsbereich für HUMAN-Modus (Millisekunden)
HUMAN_DELAY_MIN_MS: int = 20
HUMAN_DELAY_MAX_MS: int = 80

# Timeout für wait_for_page_ready → Network-Idle-Phase (Sekunden).
# Kurz halten — SPAs erfüllen networkidle selten vollständig.
# Also: fast-path in wait_for_page_ready() SKIPS networkidle entirely when
# document.readyState is already "complete" — so this only fires on fresh loads.
PAGE_READY_NETWORK_IDLE_TIMEOUT: float = 1.5

# Timeout für den vollständigen domcontentloaded-Zustand (Sekunden).
PAGE_READY_DOM_TIMEOUT: float = 8.0

# Wie lange der DOM-Stabilitäts-Check auf Mutation-Ruhe wartet (Millisekunden).
# Danach: kein weiteres Warten, egal ob DOM noch aktiv ist.
# Optimized JS: a completely stable DOM resolves in ~50ms (one poll cycle),
# NOT the full observe_ms — so this only applies when mutations are seen.
DOM_STABILITY_OBSERVE_MS: int = 200
