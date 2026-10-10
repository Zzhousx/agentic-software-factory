// Locator helpers for generated acceptance tests. Names are matched exactly (case-insensitive,
// whitespace-tolerant) and must resolve to ONE visible element: duplicates are reported as
// failures because independent acceptance tests commonly use strict locators.
import { expect, Locator, Page } from '@playwright/test';

export type Scope = Page | Locator;

export function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Exact accessible-name matcher: /^\s*name\s*$/i with flexible inner whitespace. */
export function exact(name: string | RegExp): RegExp {
  if (name instanceof RegExp) return name;
  return new RegExp('^\\s*' + escapeRegExp(name.trim()).replace(/\s+/g, '\\s+') + '\\s*$', 'i');
}

/** Substring matcher for visible text content. */
export function contains(text: string | RegExp): RegExp {
  if (text instanceof RegExp) return text;
  return new RegExp(escapeRegExp(text.trim()).replace(/\s+/g, '\\s+'), 'i');
}

const CLICKABLE = ['button', 'link', 'menuitem', 'tab', 'option', 'checkbox', 'radio', 'switch', 'treeitem', 'menuitemcheckbox', 'menuitemradio'] as const;
// Named, clickable-in-practice elements that are not "controls" in ARIA terms: grid/table cells and headers
// (e.g. a grid cell named by its coordinate) and list rows. Checked only after the control roles.
const NAMED_ITEMS = ['gridcell', 'cell', 'columnheader', 'rowheader', 'row', 'listitem'] as const;
const FIELDS = ['textbox', 'searchbox', 'combobox', 'spinbutton'] as const;

async function single(candidates: Locator[], what: string): Promise<Locator> {
  const deadline = Date.now() + 5000;
  let lastCounts = '';
  while (true) {
    const counts: number[] = [];
    for (const candidate of candidates) {
      const visible = candidate.filter({ visible: true });
      const count = await visible.count().catch(() => 0);
      counts.push(count);
      if (count === 1) return visible;
      if (count > 1) throw new Error(`Ambiguous ${what}: ${count} visible elements match; names must be unique on the page`);
    }
    lastCounts = counts.join(',');
    if (Date.now() > deadline) break;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`Not found: ${what} (no visible element; candidate counts ${lastCounts})`);
}

/** A clickable control by exact accessible name (button, link, tab, menu item, ...), a named grid/table cell or
 *  list item (e.g. a cell named "A1"), or exact visible text. */
export async function control(scope: Scope, name: string | RegExp): Promise<Locator> {
  const pattern = exact(name);
  const s = scope as any;
  return single([...CLICKABLE.map((role) => s.getByRole(role, { name: pattern })),
                 ...NAMED_ITEMS.map((role) => s.getByRole(role, { name: pattern })),
                 s.getByText(pattern)], `control ${pattern}`);
}

export async function click(scope: Scope, name: string | RegExp): Promise<void> {
  await (await control(scope, name)).click();
}

/** A form field by exact label, placeholder or accessible name. */
export async function field(scope: Scope, label: string | RegExp): Promise<Locator> {
  const pattern = exact(label);
  const s = scope as any;
  return single([s.getByLabel(pattern), s.getByPlaceholder(pattern), ...FIELDS.map((role) => s.getByRole(role, { name: pattern }))], `field ${pattern}`);
}

export async function fill(scope: Scope, label: string | RegExp, value: string): Promise<void> {
  await (await field(scope, label)).fill(value);
}

export async function select(scope: Scope, label: string | RegExp, option: string): Promise<void> {
  const element = await field(scope, label);
  const tag = await element.evaluate((node) => node.tagName.toLowerCase());
  if (tag === 'select') {
    await element.selectOption({ label: option });
    return;
  }
  await element.click();
  const page: Page = typeof (scope as any).page === 'function' ? (scope as Locator).page() : (scope as Page);
  await click(page, option);
}

/** Assert some visible element shows this text (substring, case-insensitive unless a RegExp is given). */
export async function expectText(scope: Scope, text: string | RegExp): Promise<void> {
  await expect((scope as any).getByText(contains(text)).filter({ visible: true }).first()).toBeVisible();
}

export async function expectNoText(scope: Scope, text: string | RegExp): Promise<void> {
  await expect((scope as any).getByText(contains(text)).filter({ visible: true })).toHaveCount(0);
}

/** A landmark/container by role and exact name, e.g. region('dialog', 'Create item'). */
export function region(scope: Scope, role: string, name?: string | RegExp): Locator {
  return (scope as any).getByRole(role, name === undefined ? {} : { name: exact(name) });
}

/** A grid/table cell by its accessible name (e.g. a grid cell named by its coordinate "A1"). */
export function cell(scope: Scope, name: string | RegExp): Locator {
  const s = scope as any;
  return s.getByRole('gridcell', { name: exact(name) }).or(s.getByRole('cell', { name: exact(name) }));
}

/** The row/article/listitem that contains the given text. */
export function itemWith(scope: Scope, role: 'row' | 'article' | 'listitem' | 'option' | 'gridcell' | 'cell', text: string | RegExp): Locator {
  return (scope as any).getByRole(role).filter({ hasText: contains(text) });
}
