import { describe, expect, it } from 'vitest'
import {
  NOTE_MAX,
  defaultCondition,
  parseAlertForm,
  parsePriceInput,
  priceInputText,
  submitErrorMessage,
  validateAlertForm,
  type AlertFormDraft,
} from './alertForm'

const draft = (over: Partial<AlertFormDraft> = {}): AlertFormDraft => ({
  condition: 'crosses_above',
  priceText: '65000',
  frequency: 'once',
  note: '',
  ...over,
})

describe('defaultCondition', () => {
  it('crosses up for a level at or above the last price', () => {
    expect(defaultCondition(66_000, 65_000)).toBe('crosses_above')
    expect(defaultCondition(65_000, 65_000)).toBe('crosses_above')
  })

  it('crosses down for a level below the last price', () => {
    expect(defaultCondition(64_000, 65_000)).toBe('crosses_below')
  })

  it('crosses up when the last price is unknown', () => {
    expect(defaultCondition(64_000, null)).toBe('crosses_above')
    expect(defaultCondition(64_000, Number.NaN)).toBe('crosses_above')
  })
})

describe('parsePriceInput', () => {
  it('parses plain decimals, with spaces and thousands separators', () => {
    expect(parsePriceInput('65000')).toBe(65_000)
    expect(parsePriceInput(' 65,012.50 ')).toBe(65_012.5)
    expect(parsePriceInput('.00001234')).toBe(0.00001234)
    expect(parsePriceInput('12.')).toBe(12)
  })

  it('rejects empty, signed, exponent, hex, and junk input', () => {
    for (const s of ['', '  ', '-5', '+5', '1e5', '0x10', 'abc', '1.2.3', 'Infinity', '12px']) {
      expect(parsePriceInput(s)).toBeNull()
    }
  })

  it('strips separators from a formatted price', () => {
    expect(priceInputText('65,012.50')).toBe('65012.50')
    expect(parsePriceInput(priceInputText('1,234,567.8'))).toBe(1_234_567.8)
  })
})

describe('validateAlertForm', () => {
  it('accepts a positive price and a short note', () => {
    expect(validateAlertForm(draft({ note: 'breakout' }))).toEqual({})
  })

  it('requires a finite price greater than zero', () => {
    expect(validateAlertForm(draft({ priceText: '' })).price).toBe('Enter a price')
    expect(validateAlertForm(draft({ priceText: 'abc' })).price).toBe('Enter a number')
    expect(validateAlertForm(draft({ priceText: '-1' })).price).toBe('Enter a number')
    expect(validateAlertForm(draft({ priceText: '0' })).price).toBe('Price must be greater than 0')
    expect(validateAlertForm(draft({ priceText: '0.000' })).price).toBe(
      'Price must be greater than 0',
    )
  })

  it('limits the note to 200 characters', () => {
    expect(NOTE_MAX).toBe(200)
    expect(validateAlertForm(draft({ note: 'x'.repeat(200) })).note).toBeUndefined()
    expect(validateAlertForm(draft({ note: 'x'.repeat(201) })).note).toBe(
      'Note must be at most 200 characters',
    )
  })
})

describe('parseAlertForm', () => {
  it('returns the values with a numeric price and a trimmed note', () => {
    expect(
      parseAlertForm(
        draft({ condition: 'less', priceText: '1,250.5', frequency: 'every', note: '  hi  ' }),
      ),
    ).toEqual({
      ok: true,
      value: { condition: 'less', price: 1250.5, frequency: 'every', note: 'hi' },
    })
  })

  it('returns all field errors when invalid', () => {
    expect(parseAlertForm(draft({ priceText: '0', note: 'x'.repeat(201) }))).toEqual({
      ok: false,
      errors: {
        price: 'Price must be greater than 0',
        note: 'Note must be at most 200 characters',
      },
    })
  })
})

describe('submitErrorMessage', () => {
  it('uses the error message, a string, or a fallback', () => {
    expect(submitErrorMessage(new Error('Server said no'))).toBe('Server said no')
    expect(submitErrorMessage('offline')).toBe('offline')
    expect(submitErrorMessage(new Error(''))).toBe('Could not save the alert. Try again.')
    expect(submitErrorMessage(undefined)).toBe('Could not save the alert. Try again.')
  })
})
