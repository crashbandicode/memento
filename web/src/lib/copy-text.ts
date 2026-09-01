export async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    const writeResult = navigator.clipboard.writeText(value);
    if (!writeResult || typeof writeResult.then !== "function") {
      throw new Error("Clipboard write was intercepted");
    }
    await writeResult;
    return;
  }

  const input = document.createElement("textarea");
  input.value = value;
  input.readOnly = true;
  input.tabIndex = -1;
  input.setAttribute("aria-hidden", "true");
  input.style.position = "fixed";
  input.style.left = "-9999px";
  input.style.top = "0";
  input.style.opacity = "0";
  input.style.pointerEvents = "none";
  const activeElement = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  const selection = document.getSelection();
  const selectedRanges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange())
    : [];
  document.body.appendChild(input);
  let copied = false;
  try {
    input.focus({ preventScroll: true });
    input.select();
    input.setSelectionRange(0, input.value.length);
    copied = document.execCommand("copy");
  } finally {
    input.remove();
    if (selection) {
      selection.removeAllRanges();
      selectedRanges.forEach((range) => selection.addRange(range));
    }
    activeElement?.focus({ preventScroll: true });
  }
  if (!copied) {
    throw new Error("Clipboard unavailable");
  }
}
