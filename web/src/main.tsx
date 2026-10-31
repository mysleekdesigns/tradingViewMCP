import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Bundled IBM Plex (latin subset) so the app needs no third-party font host. Every weight here is
// in use (sans 400/500/600; mono 400 for prices, 500 in stat values and keys, 600 in badges and
// panel symbols), so dropping one would change how text renders. index.html preloads the two body
// faces, sans 400 and mono 400.
import '@fontsource/ibm-plex-sans/latin-400.css'
import '@fontsource/ibm-plex-sans/latin-500.css'
import '@fontsource/ibm-plex-sans/latin-600.css'
import '@fontsource/ibm-plex-mono/latin-400.css'
import '@fontsource/ibm-plex-mono/latin-500.css'
import '@fontsource/ibm-plex-mono/latin-600.css'
import './index.css'
import App from './App.tsx'
import { ErrorBoundary } from './components/ErrorBoundary'
import { panelLoadInFlight } from './components/lazyLoad'
import { ErrorView } from './components/StatusViews'
import { applyCandleColorsToDocument, applyThemeToDocument, loadSettings } from './state/settings'

// A lazily loaded panel (components/lazyPanels.tsx) fails to load when the app was rebuilt while
// this tab stayed open: its old hashed chunk is gone. Reload to pick up the new build instead of
// failing; a second failure within a minute is left to surface (the panel's "Couldn't load"
// state, LazyBoundary), so a broken build cannot loop.
//
// Only while a panel the user opened is loading (`panelLoadInFlight`). The rail also preloads
// panels on hover and focus (SidebarRail), and Vite fires this event for those imports too; after
// a rebuild every panel chunk has a new hash, so without the check merely pointing at a rail
// button would reload the page. A failed preload is swallowed; the click that follows loads for
// real and comes back here.
//
// Vite fires the event for any rejection of the dynamic import, including a chunk that arrived
// but threw while evaluating. That reloads once too: the event cannot tell the two apart, one
// reload costs little, and the 60 s guard then lets the panel show its error state.
const CHUNK_RELOAD_KEY = 'tv.chunkReloadAt'
const CHUNK_RELOAD_GAP_MS = 60_000
window.addEventListener('vite:preloadError', () => {
  if (!panelLoadInFlight()) return
  try {
    const last = Number(sessionStorage.getItem(CHUNK_RELOAD_KEY) ?? 0)
    if (Date.now() - last < CHUNK_RELOAD_GAP_MS) return
    sessionStorage.setItem(CHUNK_RELOAD_KEY, String(Date.now()))
  } catch {
    return // no sessionStorage: no loop guard, so no automatic reload either
  }
  location.reload()
})

// Theme and candle colours go on <html> before the first render, so a light-theme user never
// sees a dark frame. App re-applies them on every Settings change.
const initialSettings = loadSettings()
applyThemeToDocument(initialSettings.theme)
applyCandleColorsToDocument(initialSettings.upColor, initialSettings.downColor)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* Last resort: a render error no inner boundary contains shows a reload card instead of a
        blank page. It renders nothing of its own while the app works. */}
    <ErrorBoundary
      fallback={() => (
        <ErrorView
          title="Something went wrong"
          message="The app hit an unexpected error. Reload the page to start it again."
          onRetry={() => location.reload()}
          retryDisabled={false}
        />
      )}
    >
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
