import { createEffect, createSignal, onCleanup, onMount } from "solid-js";
import type { Track } from "../lib/api";
import { formatTime } from "../lib/api";

/**
 * The set, drawn.
 *
 * Three layers stacked in one canvas so they stay pixel-aligned under zoom:
 *
 *  1. A segment ribbon — one block per detected track, numbered. Identified
 *     blocks carry the accent, unidentified ones stay grey and hollow. This
 *     is the layer that answers "what did it find, and where" at a glance.
 *  2. The RMS envelope, mirrored around the centre line. The played portion is
 *     lit; the rest is dimmed, so position reads without hunting for the
 *     playhead.
 *  3. A time ruler with ticks chosen from the visible span, not a fixed count.
 *
 * Unidentified stretches are drawn, never hidden. A run nobody can name is
 * usually a dub, an edit or an unsigned promo — the most interesting thing in
 * the set, and the reason to keep digging.
 */

const RIBBON_H = 22;
const RULER_H = 22;
const GAP = 6;

interface Props {
  waveform: number[];
  tracks: Track[];
  duration: number;
  currentTime: number;
  activeIndex: number | null;
  height?: number;
  onSeek: (time: number) => void;
  onSelect?: (index: number) => void;
}

interface View {
  start: number;
  end: number;
}

export default function Waveform(props: Props) {
  let canvas!: HTMLCanvasElement;
  let host!: HTMLDivElement;

  const [view, setView] = createSignal<View>({ start: 0, end: 0 });
  const [hover, setHover] = createSignal<{ x: number; time: number } | null>(null);
  const [size, setSize] = createSignal({ width: 800, height: 150 });
  const [dragging, setDragging] = createSignal(false);

  const waveHeight = () => props.height ?? 148;
  const totalHeight = () => RIBBON_H + GAP + waveHeight() + GAP + RULER_H;

  // Reset the viewport whenever a different set is loaded.
  createEffect(() => {
    const duration = props.duration;
    if (duration > 0) setView({ start: 0, end: duration });
  });

  const span = () => {
    const v = view();
    return Math.max(0.001, v.end - v.start);
  };

  const timeToX = (time: number, width: number) =>
    ((time - view().start) / span()) * width;
  const xToTime = (x: number, width: number) =>
    view().start + (x / width) * span();

  /**
   * Live width, straight from layout.
   *
   * The `size` signal exists only to trigger a redraw; it must never be the
   * source of truth for geometry. ResizeObserver callbacks are tied to the
   * rendering pipeline, so they do not fire while the tab is in the
   * background — mount there and the signal keeps its initial placeholder,
   * which silently skews every click-to-seek by the ratio between the two.
   */
  const liveWidth = () => {
    const measured = canvas?.getBoundingClientRect().width ?? 0;
    return measured > 0 ? measured : size().width;
  };

  onMount(() => {
    // Measure once, synchronously, so the first paint and the first click are
    // both correct even if no observer callback ever arrives.
    const initial = host.clientWidth;
    if (initial > 0) setSize({ width: initial, height: totalHeight() });

    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect && rect.width > 0) {
        setSize({ width: rect.width, height: totalHeight() });
      }
    });
    observer.observe(host);

    // Window resizes are delivered even when observers are throttled.
    const onResize = () => {
      const width = host.clientWidth;
      if (width > 0) setSize({ width, height: totalHeight() });
    };
    window.addEventListener("resize", onResize);

    onCleanup(() => {
      observer.disconnect();
      window.removeEventListener("resize", onResize);
    });
  });

  createEffect(() => {
    // Every signal read inside draw() registers here, so the canvas repaints
    // on playhead moves, zoom, hover and data changes without any manual
    // invalidation.
    draw();
  });

  function draw() {
    size();                 // reactive trigger only
    const width = liveWidth();
    const h = waveHeight();
    const total = totalHeight();
    if (!canvas || width <= 0) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(total * dpr);
    canvas.style.height = `${total}px`;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, total);

    const peaks = props.waveform;
    const duration = props.duration;
    const v = view();
    const waveTop = RIBBON_H + GAP;
    const mid = waveTop + h / 2;
    const playX = timeToX(props.currentTime, width);

    // ── Layer 1: segment ribbon ──────────────────────────────────────
    props.tracks.forEach((track, i) => {
      const x0 = timeToX(track.start, width);
      const x1 = timeToX(track.end, width);
      if (x1 < -2 || x0 > width + 2) return;

      const left = Math.max(0, x0);
      const w = Math.min(width, x1) - left;
      if (w <= 0) return;

      const active = props.activeIndex === i;
      if (track.identified) {
        ctx.fillStyle = active
          ? "rgba(255,85,0,0.55)"
          : i % 2 === 0
            ? "rgba(255,85,0,0.24)"
            : "rgba(255,85,0,0.16)";
      } else {
        ctx.fillStyle = active ? "rgba(154,145,136,0.4)" : "rgba(154,145,136,0.13)";
      }
      roundRect(ctx, left + 0.5, 2, Math.max(1, w - 1), RIBBON_H - 4, 3);
      ctx.fill();

      if (!track.identified) {
        ctx.strokeStyle = "rgba(154,145,136,0.35)";
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        roundRect(ctx, left + 0.5, 2, Math.max(1, w - 1), RIBBON_H - 4, 3);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      // Number the block only when it can actually be read.
      if (w > 22) {
        ctx.fillStyle = track.identified
          ? "rgba(255,255,255,0.92)"
          : "rgba(237,231,224,0.6)";
        ctx.font = "600 10px 'JetBrains Mono', monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(
          track.identified ? String(track.index) : "?",
          left + w / 2,
          RIBBON_H / 2,
        );
      }
    });

    // ── Layer 2: waveform ────────────────────────────────────────────
    if (peaks.length > 0 && duration > 0) {
      const barW = 2;
      const gap = 1;
      const step = barW + gap;
      const bars = Math.floor(width / step);

      for (let i = 0; i < bars; i++) {
        const x = i * step;
        const t = xToTime(x + barW / 2, width);
        if (t < 0 || t > duration) continue;

        // Sample the envelope across the pixel's own time span, so zooming
        // out never aliases a transient away.
        const t0 = xToTime(x, width);
        const t1 = xToTime(x + barW, width);
        const i0 = Math.max(0, Math.floor((t0 / duration) * peaks.length));
        const i1 = Math.min(peaks.length, Math.ceil((t1 / duration) * peaks.length));

        let peak = 0;
        for (let k = i0; k < i1; k++) peak = Math.max(peak, peaks[k] ?? 0);
        if (i1 <= i0) peak = peaks[Math.min(peaks.length - 1, i0)] ?? 0;

        const amp = Math.max(1, peak * (h / 2) * 0.94);
        const seg = segmentAt(props.tracks, t);
        const played = t <= props.currentTime;
        const isActive = seg !== null && props.activeIndex === seg;
        const identified = seg !== null && props.tracks[seg]?.identified;

        if (played) {
          ctx.fillStyle = identified
            ? isActive
              ? "#ff8a45"
              : "#ff5500"
            : "#8a8078";
        } else {
          ctx.fillStyle = identified
            ? isActive
              ? "rgba(255,124,51,0.6)"
              : "rgba(255,85,0,0.36)"
            : "rgba(138,128,120,0.3)";
        }
        ctx.fillRect(x, mid - amp, barW, amp * 2);
      }
    } else {
      ctx.fillStyle = "rgba(154,145,136,0.18)";
      ctx.fillRect(0, mid - 1, width, 2);
    }

    // Centre line
    ctx.fillStyle = "rgba(237,231,224,0.07)";
    ctx.fillRect(0, mid, width, 1);

    // ── Segment boundaries ───────────────────────────────────────────
    ctx.strokeStyle = "rgba(237,231,224,0.14)";
    ctx.lineWidth = 1;
    props.tracks.forEach((track) => {
      const x = Math.round(timeToX(track.start, width)) + 0.5;
      if (x < 0 || x > width || track.start <= 0) return;
      ctx.beginPath();
      ctx.moveTo(x, waveTop);
      ctx.lineTo(x, waveTop + h);
      ctx.stroke();
    });

    // ── Layer 3: ruler ───────────────────────────────────────────────
    const rulerY = waveTop + h + GAP;
    ctx.fillStyle = "rgba(237,231,224,0.08)";
    ctx.fillRect(0, rulerY, width, 1);

    const stepSeconds = niceStep(span(), width);
    ctx.font = "500 10px 'JetBrains Mono', monospace";
    ctx.textBaseline = "top";
    const first = Math.ceil(v.start / stepSeconds) * stepSeconds;
    for (let t = first; t <= v.end; t += stepSeconds) {
      const x = timeToX(t, width);
      if (x < 12 || x > width - 12) continue;
      ctx.fillStyle = "rgba(154,145,136,0.35)";
      ctx.fillRect(Math.round(x) + 0.5, rulerY, 1, 4);
      ctx.fillStyle = "rgba(154,145,136,0.75)";
      ctx.textAlign = "center";
      ctx.fillText(formatTime(t), x, rulerY + 7);
    }

    // ── Hover crosshair ──────────────────────────────────────────────
    const hovered = hover();
    if (hovered && !dragging()) {
      ctx.strokeStyle = "rgba(237,231,224,0.3)";
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(Math.round(hovered.x) + 0.5, 0);
      ctx.lineTo(Math.round(hovered.x) + 0.5, waveTop + h);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // ── Playhead, drawn last so nothing covers it ────────────────────
    if (playX >= -1 && playX <= width + 1) {
      ctx.fillStyle = "#ede7e0";
      ctx.fillRect(Math.round(playX) - 1, 0, 2, waveTop + h);
      ctx.beginPath();
      ctx.moveTo(playX - 6, 0);
      ctx.lineTo(playX + 6, 0);
      ctx.lineTo(playX, 9);
      ctx.closePath();
      ctx.fill();
    }
  }

  // ── Interaction ────────────────────────────────────────────────────

  function localX(event: MouseEvent): number {
    const rect = canvas.getBoundingClientRect();
    return Math.max(0, Math.min(rect.width, event.clientX - rect.left));
  }

  function onPointerMove(event: MouseEvent) {
    const x = localX(event);
    setHover({ x, time: xToTime(x, liveWidth()) });
  }

  function onClick(event: MouseEvent) {
    const time = xToTime(localX(event), liveWidth());
    props.onSeek(Math.max(0, Math.min(props.duration, time)));
    const index = segmentAt(props.tracks, time);
    if (index !== null) props.onSelect?.(index);
  }

  function onWheel(event: WheelEvent) {
    if (props.duration <= 0) return;
    event.preventDefault();

    const width = liveWidth();
    const anchor = xToTime(localX(event), width);
    const factor = event.deltaY > 0 ? 1.25 : 0.8;
    const nextSpan = Math.max(
      // Never zoom past ~5 seconds across the full width — beyond that the
      // envelope has no more detail to show.
      Math.min(5, props.duration),
      Math.min(props.duration, span() * factor),
    );

    // Keep the time under the cursor pinned while zooming.
    const ratio = (anchor - view().start) / span();
    let start = anchor - ratio * nextSpan;
    let end = start + nextSpan;
    if (start < 0) { start = 0; end = nextSpan; }
    if (end > props.duration) { end = props.duration; start = end - nextSpan; }
    setView({ start: Math.max(0, start), end });
  }

  function onPointerDown(event: MouseEvent) {
    if (event.button !== 1 && !event.shiftKey) return; // middle-drag or shift-drag pans
    event.preventDefault();
    setDragging(true);
    const startX = event.clientX;
    const origin = view();

    const move = (e: MouseEvent) => {
      const width = liveWidth();
      const delta = ((startX - e.clientX) / width) * (origin.end - origin.start);
      let start = origin.start + delta;
      let end = origin.end + delta;
      if (start < 0) { end -= start; start = 0; }
      if (end > props.duration) { start -= end - props.duration; end = props.duration; }
      setView({ start: Math.max(0, start), end: Math.min(props.duration, end) });
    };
    const up = () => {
      setDragging(false);
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  const zoomed = () => props.duration > 0 && span() < props.duration - 0.5;

  const hoveredTrack = () => {
    const h = hover();
    if (!h) return null;
    const index = segmentAt(props.tracks, h.time);
    return index === null ? null : props.tracks[index] ?? null;
  };

  return (
    <div class="waveform" ref={host}>
      <canvas
        ref={canvas}
        class="waveform-canvas"
        style={{ cursor: dragging() ? "grabbing" : "pointer" }}
        onMouseMove={onPointerMove}
        onMouseLeave={() => setHover(null)}
        onClick={onClick}
        onWheel={onWheel}
        onMouseDown={onPointerDown}
        role="slider"
        tabindex="0"
        aria-label="Set waveform — click to seek"
        aria-valuemin={0}
        aria-valuemax={Math.round(props.duration)}
        aria-valuenow={Math.round(props.currentTime)}
        aria-valuetext={`${formatTime(props.currentTime)} of ${formatTime(props.duration)}`}
      />

      {hover() && (
        <div
          class="waveform-tip"
          style={{
            left: `${Math.min(Math.max(hover()!.x, 70), liveWidth() - 70)}px`,
          }}
        >
          <span class="waveform-tip-time mono">{formatTime(hover()!.time)}</span>
          {hoveredTrack() && (
            <span class="waveform-tip-track">
              {hoveredTrack()!.identified
                ? `${hoveredTrack()!.artist} — ${hoveredTrack()!.title}`
                : "Unidentified"}
            </span>
          )}
        </div>
      )}

      <div class="waveform-hint tiny faint">
        {zoomed() ? (
          <>
            <button
              class="btn btn-ghost btn-sm"
              onClick={() => setView({ start: 0, end: props.duration })}
            >
              Reset zoom
            </button>
            <span>
              Showing {formatTime(view().start)}–{formatTime(view().end)}
            </span>
          </>
        ) : (
          <span>Scroll to zoom · shift-drag to pan · click to seek</span>
        )}
      </div>
    </div>
  );
}

function segmentAt(tracks: Track[], time: number): number | null {
  for (let i = 0; i < tracks.length; i++) {
    const t = tracks[i]!;
    if (time >= t.start && time < t.end) return i;
  }
  return tracks.length > 0 && time >= (tracks[tracks.length - 1]?.end ?? 0)
    ? tracks.length - 1
    : null;
}

/** Pick a tick interval that yields readable, round labels at this zoom. */
function niceStep(spanSeconds: number, width: number): number {
  const target = spanSeconds / Math.max(4, Math.floor(width / 90));
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
  for (const step of steps) if (target <= step) return step;
  return 7200;
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number, y: number, w: number, h: number, r: number,
) {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}
