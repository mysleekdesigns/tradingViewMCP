import { createElement, type ErrorInfo } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { ErrorBoundary, type ErrorBoundaryProps } from './ErrorBoundary'

// The test environment is node (no DOM), so the boundary's lifecycle is driven by hand:
// React calls getDerivedStateFromError, then render, then componentDidCatch.
function boundary(over: Partial<ErrorBoundaryProps> = {}) {
  const props: ErrorBoundaryProps = {
    children: createElement('p', null, 'child'),
    fallback: ({ error }) => createElement('p', { role: 'alert' }, `failed: ${error.message}`),
    ...over,
  }
  const b = new ErrorBoundary(props)
  // setState needs a mounted updater; record what it would set instead.
  b.setState = ((s: Partial<typeof b.state>) => {
    b.state = { ...b.state, ...s }
  }) as typeof b.setState
  return b
}

describe('ErrorBoundary', () => {
  it('renders its children while nothing failed', () => {
    const html = renderToStaticMarkup(
      createElement(ErrorBoundary, {
        fallback: () => 'fallback',
        children: createElement('p', null, 'research'),
      }),
    )
    expect(html).toBe('<p>research</p>')
  })

  it('swaps in the fallback after a render error, and reset brings the children back', () => {
    const b = boundary()
    b.state = ErrorBoundary.getDerivedStateFromError(new Error('bad payload'))
    const out = b.render() as { props: { children: string } }
    expect(out.props.children).toBe('failed: bad payload')
    b.reset()
    expect(b.state.error).toBeNull()
    expect((b.render() as { props: { children: string } }).props.children).toBe('child')
  })

  it('passes reset to the fallback', () => {
    const fallback = vi.fn(() => null)
    const b = boundary({ fallback })
    b.state = ErrorBoundary.getDerivedStateFromError('thrown string')
    expect(b.render()).toBeNull()
    expect(fallback).toHaveBeenCalledWith({ error: new Error('thrown string'), reset: b.reset })
  })

  it('reports the error through onError (else console.error)', () => {
    const onError = vi.fn()
    const info = { componentStack: '\n at X' } as ErrorInfo
    boundary({ onError }).componentDidCatch(new Error('x'), info)
    expect(onError).toHaveBeenCalledWith(new Error('x'), info)

    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    boundary().componentDidCatch(new Error('y'), info)
    expect(spy).toHaveBeenCalledOnce()
    spy.mockRestore()
  })

  it('turns any thrown value into an Error', () => {
    const e = new Error('e')
    const derive = (v: unknown) => ErrorBoundary.getDerivedStateFromError(v).error
    expect(derive(e)).toBe(e)
    expect(derive('s')?.message).toBe('s')
    expect(derive({})?.message).toBe('Unknown render error')
    expect(derive(undefined)?.message).toBe('Unknown render error')
  })
})
