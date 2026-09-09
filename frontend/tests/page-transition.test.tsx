import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { FocusShell } from '../src/components/FocusShell';

const originalAnimate = Object.getOwnPropertyDescriptor(Element.prototype, 'animate');
let reduced = false;
const addListener = vi.fn();
const removeListener = vi.fn();
const animate = vi.fn(() => ({ cancel: vi.fn() }));

beforeEach(() => {
  reduced = false;
  animate.mockClear();
  addListener.mockClear();
  removeListener.mockClear();
  Object.defineProperty(Element.prototype, 'animate', { configurable: true, value: animate });
  vi.stubGlobal('matchMedia', () => ({ matches: reduced, addEventListener: addListener, removeEventListener: removeListener }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  if (originalAnimate) Object.defineProperty(Element.prototype, 'animate', originalAnimate);
  else delete (Element.prototype as Partial<Element>).animate;
});

function shell(page: string) {
  return <FocusShell projectId="motion-test" transitionKey={page} nodes={[]} selectedNodeId={null}
    onSelectNode={() => {}} rail={null} toolbar={null} sidebar={null} inspector={null}>
    <textarea aria-label="临时草稿" defaultValue="" />
  </FocusShell>;
}

it('animates only page changes, preserves the existing input, and cancels superseded transitions', () => {
  const { rerender, unmount } = render(shell('write'));
  const draft = screen.getByRole('textbox');
  fireEvent.change(draft, { target: { value: '未保存的想法' } });
  rerender(shell('write'));
  expect(animate).not.toHaveBeenCalled();
  rerender(shell('library'));
  expect(animate).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('textbox')).toBe(draft);
  expect(draft).toHaveValue('未保存的想法');
  const first = animate.mock.results[0].value;
  rerender(shell('settings'));
  expect(first.cancel).toHaveBeenCalled();
  expect(animate).toHaveBeenCalledTimes(2);
  const last = animate.mock.results[1].value;
  unmount();
  expect(last.cancel).toHaveBeenCalled();
});

it('skips motion when requested and cancels immediately when the system preference changes', () => {
  const { rerender } = render(shell('write'));
  reduced = true;
  rerender(shell('library'));
  expect(animate).not.toHaveBeenCalled();
  reduced = false;
  rerender(shell('settings'));
  const animation = animate.mock.results[0].value;
  act(() => addListener.mock.calls[0][1]({ matches: true }));
  expect(animation.cancel).toHaveBeenCalled();
  rerender(shell('write'));
  expect(removeListener).toHaveBeenCalled();
});
