export function clamp(value, lo, hi) {
  return Math.max(lo, Math.min(hi, value));
}

export function normalizedWheelSteps(deltaY, deltaMode = 0, pagePixels = 800) {
  let delta = Number(deltaY);
  if (!Number.isFinite(delta) || delta === 0) return 0;
  if (deltaMode === 1) delta *= 40; // WheelEvent.DOM_DELTA_LINE
  else if (deltaMode === 2) delta *= Math.max(1, Number(pagePixels) || 1); // DOM_DELTA_PAGE
  // One conventional browser wheel detent is commonly around 100–120 px.
  // Keep fractional values for precision trackpads, while clamping pathological
  // spikes so one event cannot teleport across the entire timeline.
  return clamp(-delta / 120, -4, 4);
}

export function zoomWindow(start, end, factor, anchor = 0.5) {
  anchor = clamp(Number(anchor), 0, 1);
  const span = Math.max(0, Number(end) - Number(start));
  const target = Number(start) + span * anchor;
  const newSpan = span * Number(factor);
  return [
    target - newSpan * anchor,
    target + newSpan * (1 - anchor),
  ];
}

export function verticalScaleFromWheel(
  current,
  steps,
  minimum = 0.1,
  maximum = 32,
  base = 1.15,
) {
  const value = Number(current) * (Number(base) ** (-Number(steps)));
  if (!Number.isFinite(value)) return clamp(Number(current), minimum, maximum);
  return clamp(value, minimum, maximum);
}
