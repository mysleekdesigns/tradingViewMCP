import { Component, type ErrorInfo, type ReactNode } from 'react'

export interface ErrorBoundaryFallbackProps {
  error: Error
  /** Clear the error and mount the children again. */
  reset(): void
}

export interface ErrorBoundaryProps {
  children: ReactNode
  /** Rendered in place of the children after one of them threw while rendering. */
  fallback(props: ErrorBoundaryFallbackProps): ReactNode
  /** Where the error is reported; defaults to `console.error`. */
  onError?(error: Error, info: ErrorInfo): void
}

interface ErrorBoundaryState {
  error: Error | null
}

/** Any thrown value as an Error (a render can throw a string or an object). */
function toError(value: unknown): Error {
  if (value instanceof Error) return value
  return new Error(typeof value === 'string' && value !== '' ? value : 'Unknown render error')
}

/**
 * Contains a render error to one part of the app (e.g. the Research view), so a bad response
 * shows a small "failed to render" panel instead of unmounting the whole app. React only
 * supports error boundaries as class components.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { error: toError(error) }
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    const err = toError(error)
    if (this.props.onError) this.props.onError(err, info)
    else console.error('A view failed to render', err, info.componentStack)
  }

  reset = (): void => {
    this.setState({ error: null })
  }

  render(): ReactNode {
    const { error } = this.state
    if (error !== null) return this.props.fallback({ error, reset: this.reset })
    return this.props.children
  }
}
