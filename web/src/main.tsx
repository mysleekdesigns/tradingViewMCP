import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Bundled IBM Plex (latin subset, weights in use) so the app needs no third-party font host.
import '@fontsource/ibm-plex-sans/latin-400.css'
import '@fontsource/ibm-plex-sans/latin-500.css'
import '@fontsource/ibm-plex-sans/latin-600.css'
import '@fontsource/ibm-plex-mono/latin-400.css'
import '@fontsource/ibm-plex-mono/latin-500.css'
import '@fontsource/ibm-plex-mono/latin-600.css'
import './index.css'
import App from './App.tsx'
import { applyCandleColorsToDocument, applyThemeToDocument, loadSettings } from './state/settings'

// Theme and candle colours go on <html> before the first render, so a light-theme user never
// sees a dark frame. App re-applies them on every Settings change.
const initialSettings = loadSettings()
applyThemeToDocument(initialSettings.theme)
applyCandleColorsToDocument(initialSettings.upColor, initialSettings.downColor)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
