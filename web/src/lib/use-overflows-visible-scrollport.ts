"use client";

import { type RefObject, useEffect, useState } from "react";

type ResizeCallback = () => void;

const resizeCallbacks = new Map<Element, Set<ResizeCallback>>();
let sharedResizeObserver: ResizeObserver | null = null;
const viewportCallbacks = new Set<ResizeCallback>();
let viewportCleanup: (() => void) | null = null;

function observeResize(element: Element, callback: ResizeCallback) {
  if (typeof ResizeObserver === "undefined") return () => undefined;
  if (!sharedResizeObserver) {
    sharedResizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        resizeCallbacks.get(entry.target)?.forEach((notify) => notify());
      }
    });
  }
  const callbacks = resizeCallbacks.get(element) ?? new Set<ResizeCallback>();
  callbacks.add(callback);
  resizeCallbacks.set(element, callbacks);
  sharedResizeObserver.observe(element);

  return () => {
    const current = resizeCallbacks.get(element);
    current?.delete(callback);
    if (current?.size) return;
    resizeCallbacks.delete(element);
    sharedResizeObserver?.unobserve(element);
    if (resizeCallbacks.size === 0) {
      sharedResizeObserver?.disconnect();
      sharedResizeObserver = null;
    }
  };
}

function subscribeViewport(callback: ResizeCallback) {
  viewportCallbacks.add(callback);
  if (!viewportCleanup) {
    const notify = () => viewportCallbacks.forEach((subscriber) => subscriber());
    window.addEventListener("resize", notify);
    window.visualViewport?.addEventListener("resize", notify);
    viewportCleanup = () => {
      window.removeEventListener("resize", notify);
      window.visualViewport?.removeEventListener("resize", notify);
    };
  }

  return () => {
    viewportCallbacks.delete(callback);
    if (viewportCallbacks.size > 0) return;
    viewportCleanup?.();
    viewportCleanup = null;
  };
}

function potentialScrollports(element: HTMLElement): HTMLElement[] {
  const ancestors: HTMLElement[] = [];
  let ancestor = element.parentElement;
  while (ancestor && ancestor !== document.documentElement) {
    if (
      ancestor !== document.body
      && /auto|scroll|overlay/.test(window.getComputedStyle(ancestor).overflowY)
    ) {
      ancestors.push(ancestor);
    }
    ancestor = ancestor.parentElement;
  }
  return ancestors;
}

function visibleCapacity(scrollports: HTMLElement[], gutter: number, headerSelector: string) {
  const visualViewport = window.visualViewport;
  const viewportTop = visualViewport?.offsetTop ?? 0;
  const viewportHeight = visualViewport?.height ?? window.innerHeight;
  const viewportBottom = viewportTop + viewportHeight;
  let visibleTop = viewportTop;

  const header = document.querySelector<HTMLElement>(headerSelector);
  if (header && /fixed|sticky/.test(window.getComputedStyle(header).position)) {
    const headerRect = header.getBoundingClientRect();
    if (headerRect.top <= viewportTop + 1 && headerRect.bottom > viewportTop) {
      visibleTop = Math.min(viewportBottom, headerRect.bottom);
    }
  }

  let capacity = Math.max(0, viewportBottom - visibleTop);
  for (const scrollport of scrollports) {
    if (scrollport.scrollHeight > scrollport.clientHeight + 1) {
      // Capacity is stable while the document scrolls; current intersection
      // would misclassify a reader that merely starts below the fold.
      capacity = Math.min(capacity, scrollport.clientHeight);
    }
  }
  return Math.max(0, capacity - gutter);
}

/**
 * Reports whether an element is taller than the viewport/scrollport in which
 * it can be viewed. All hook instances share one observer and viewport
 * listener, so long virtualized feeds do not create listener-per-row churn.
 */
export function useOverflowsVisibleScrollport(
  ref: RefObject<HTMLElement | null>,
  { gutter = 8, headerSelector = "header" }: { gutter?: number; headerSelector?: string } = {},
) {
  const [overflows, setOverflows] = useState(false);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const scrollports = potentialScrollports(element);
    let animationFrame = 0;
    const measure = () => {
      window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(() => {
        setOverflows(
          element.getBoundingClientRect().height
            > visibleCapacity(scrollports, gutter, headerSelector),
        );
      });
    };

    const cleanups = [
      observeResize(element, measure),
      ...scrollports.map((scrollport) => observeResize(scrollport, measure)),
      subscribeViewport(measure),
    ];
    measure();
    return () => {
      window.cancelAnimationFrame(animationFrame);
      cleanups.forEach((cleanup) => cleanup());
    };
  }, [gutter, headerSelector, ref]);

  return overflows;
}
