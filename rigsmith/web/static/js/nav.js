/* Moving between panels, carrying something with you.
 *
 * The work here runs across panels - research a tone, find a capture for the
 * amp, compile it into a rig, test the rig against the record - and a set of
 * tabs makes each of those steps look like a separate place you have to know
 * to visit. This is the thread between them: a panel can hand the next panel
 * the thing it just produced, and the tab switch comes along for free.
 *
 * Deliberately an event rather than direct imports between panels. Panels
 * importing each other would make the graph a knot, and a panel that is not
 * listening should be a no-op, not a crash.
 */

const EVENT = "rigsmith:navigate";

/**
 * @param {string} panel   the panel's data-panel name
 * @param {object} payload anything the destination knows how to receive
 */
export function goTo(panel, payload = {}) {
  document.dispatchEvent(new CustomEvent(EVENT, { detail: { panel, payload } }));
}

/**
 * Handle arriving at a panel with a payload. The handler runs *after* the
 * panel is visible, so it can measure and focus things.
 */
export function onArrive(panel, handler) {
  document.addEventListener(EVENT, (event) => {
    if (event.detail.panel !== panel) return;
    // A microtask is enough: the panel is shown synchronously by app.js
    // before the rest of the listeners run.
    queueMicrotask(() => handler(event.detail.payload));
  });
}

/** A button that carries something to another panel. */
export function crossLink(label, panel, payload, { title = null } = {}) {
  const button = document.createElement("button");
  button.className = "btn small";
  button.textContent = label;
  if (title) button.title = title;
  button.onclick = () => goTo(panel, payload);
  return button;
}
