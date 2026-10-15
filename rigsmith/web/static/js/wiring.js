/* A wiring diagram for a loop route: which jack goes where.
 *
 * Getting the loop wrong is silent - the Core's USB channels 1-2 bypass the
 * rig, a return on the wrong input is just quiet - so each route can draw
 * itself: the computer (or interface) on the left, the Core on the right,
 * and every cable or USB channel the route uses between them, labelled with
 * the channel numbers it was saved with.
 */

const NS = "http://www.w3.org/2000/svg";

function node(tag, attrs = {}, text = null) {
  const element = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  if (text !== null) element.textContent = text;
  return element;
}

function box(svg, x, y, w, h, title, subtitle) {
  svg.append(node("rect", { x, y, width: w, height: h, rx: 8, class: "w-box" }));
  svg.append(node("text", { x: x + w / 2, y: y + 22, "text-anchor": "middle", class: "w-title" }, title));
  if (subtitle) svg.append(node("text", { x: x + w / 2, y: y + 40, "text-anchor": "middle", class: "w-sub" }, subtitle));
}

function jack(svg, x, y, label, side) {
  svg.append(node("circle", { cx: x, cy: y, r: 5, class: "w-jack" }));
  svg.append(node("text", { x: side === "left" ? x - 10 : x + 10, y: y + 4, "text-anchor": side === "left" ? "end" : "start", class: "w-label" }, label));
}

function wire(svg, x1, y1, x2, y2, label, kind) {
  const mid = (x1 + x2) / 2;
  svg.append(node("path", { d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`, class: `w-wire ${kind}`, "marker-end": "url(#w-arrow)" }));
  svg.append(node("text", { x: mid, y: (y1 + y2) / 2 - 6, "text-anchor": "middle", class: "w-wire-label" }, label));
}

/** The box a device belongs to - "Playback 1/2 (E2x2)" is the E2x2. */
function shortName(name) {
  const text = name || "";
  const match = /^(.*?)\s*\(([^()]+)\)\s*$/.exec(text);
  const box = match && match[1].toLowerCase() !== match[2].toLowerCase() ? match[2] : text;
  return box.length > 30 ? `${box.slice(0, 29)}...` : box;
}

/** An <svg> showing how `route` is wired. */
export function wiringDiagram(route) {
  const svg = node("svg", { viewBox: "0 0 640 205", class: "wiring", role: "img" });
  const defs = node("defs");
  const marker = node("marker", { id: "w-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
  marker.append(node("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "w-arrowhead" }));
  defs.append(marker);
  svg.append(defs);

  const outs = (route.output_channels || []).join(",");
  const ins = (route.input_channels || []).join(",");
  const left = route.kind === "usb-reamp" ? "Computer" : "Interface";
  box(svg, 20, 20, 200, 155, left, route.kind === "usb-reamp" ? "USB to the Core" : shortName(route.output_device));
  box(svg, 420, 20, 200, 155, "HeadRush Core", route.kind === "usb-reamp" ? "USB Mode: Reamp" : "the rig you are testing");

  if (route.kind === "usb-reamp") {
    jack(svg, 220, 95, `USB out ${outs}`, "left");
    jack(svg, 420, 95, "rig in", "right");
    wire(svg, 220, 95, 420, 95, `DI on USB ch ${outs}`, "usb");
    jack(svg, 420, 145, "rig out", "right");
    jack(svg, 220, 145, `USB in ${ins}`, "left");
    wire(svg, 420, 145, 220, 145, `wet on USB ch ${ins}`, "usb");
    svg.append(node("text", { x: 320, y: 196, "text-anchor": "middle", class: "w-warn" }, "USB ch 1-2 into the Core skip the rig - the DI must go on 3"));
    return svg;
  }
  jack(svg, 220, 95, `out ${outs}`, "left");
  jack(svg, 420, 95, "guitar in", "right");
  wire(svg, 220, 95, 420, 95, "cable (a reamp box if you have one)", "cable");
  if (route.kind === "hybrid") {
    jack(svg, 420, 145, "USB", "right");
    jack(svg, 220, 145, `Core USB ${ins}`, "left");
    wire(svg, 420, 145, 220, 145, `wet on the Core's USB ${ins}${route.reference_channel ? `, dry on ${route.reference_channel}` : ""}`, "usb");
  } else {
    jack(svg, 420, 145, "1/4\" out L", "right");
    jack(svg, 220, 145, `in ${ins}`, "left");
    wire(svg, 420, 145, 220, 145, "cable back", "cable");
  }
  if (route.kind === "analog" || route.kind === "custom") {
    svg.append(node("text", { x: 320, y: 196, "text-anchor": "middle", class: "w-note" }, "one clock: both ends are the same interface"));
  } else {
    svg.append(node("text", { x: 320, y: 196, "text-anchor": "middle", class: "w-note" }, "two clocks: drift is measured from the sync marks on every pass"));
  }
  return svg;
}
