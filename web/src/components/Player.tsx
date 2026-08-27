import { createEffect, createSignal, onCleanup, onMount } from "solid-js";
import { formatTime } from "../lib/api";

/**
 * Transport for the set audio.
 *
 * The <audio> element is owned here but exposed upward, because the waveform
 * needs to seek it and the track list needs to follow it. Range requests on
 * the backend are what make a jump to 01:47:00 in a three-hour mix instant
 * instead of a full download.
 */

interface Props {
  src: string;
  duration: number;
  /** Where a shared link asked playback to begin, in seconds. */
  startAt?: number;
  /** Owned by the parent so the waveform and the transport can never disagree. */
  currentTime: number;
  onTimeUpdate: (time: number) => void;
  onReady?: (element: HTMLAudioElement) => void;
  onError?: (message: string) => void;
}

const SPEEDS = [0.75, 1, 1.25, 1.5, 2];

export default function Player(props: Props) {
  let audio!: HTMLAudioElement;

  /**
   * Start position, expressed as a media fragment on the URL.
   *
   * Browsers honour `#t=` natively and begin decoding at that offset, which
   * sidesteps the race in setting `currentTime` by hand: with preload
   * "metadata" the element has no timeline yet, so an assignment is dropped,
   * and doing it from a loadedmetadata handler means playback can start at
   * zero and jump a moment later. Read once, deliberately — re-reading it on
   * every scrub would rewrite src and reload the audio.
   */
  const initialSrc = (() => {
    const start = Math.round(props.startAt ?? 0);
    return start > 0 ? `${props.src}#t=${start}` : props.src;
  })();
  const [playing, setPlaying] = createSignal(false);
  const [volume, setVolume] = createSignal(1);
  const [speed, setSpeed] = createSignal(1);
  const [buffering, setBuffering] = createSignal(false);
  const [failed, setFailed] = createSignal(false);

  onMount(() => {
    props.onReady?.(audio);

    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;

      if (event.code === "Space") {
        event.preventDefault();
        toggle();
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        seekBy(event.shiftKey ? -60 : -10);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        seekBy(event.shiftKey ? 60 : 10);
      }
    };
    window.addEventListener("keydown", onKey);
    onCleanup(() => window.removeEventListener("keydown", onKey));
  });

  createEffect(() => {
    if (audio) audio.playbackRate = speed();
  });
  createEffect(() => {
    if (audio) audio.volume = volume();
  });

  function toggle() {
    if (!audio || failed()) return;
    if (audio.paused) {
      void audio.play().catch(() => {
        setFailed(true);
        props.onError?.("Playback was blocked. Click play again.");
      });
    } else {
      audio.pause();
    }
  }

  function seekBy(delta: number) {
    if (!audio) return;
    const ceiling = props.duration || audio.duration || 0;
    const next = Math.max(0, Math.min(ceiling, props.currentTime + delta));
    audio.currentTime = next;
    props.onTimeUpdate(next);
  }

  return (
    <div class="player">
      <audio
        ref={audio}
        src={initialSrc}
        preload="metadata"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onWaiting={() => setBuffering(true)}
        onPlaying={() => setBuffering(false)}
        onCanPlay={() => setBuffering(false)}
        onTimeUpdate={() => props.onTimeUpdate(audio.currentTime)}
        onSeeked={() => props.onTimeUpdate(audio.currentTime)}
        onError={() => {
          setFailed(true);
          props.onError?.(
            "The audio for this set is no longer available. The tracklist is unaffected.",
          );
        }}
      />

      <button
        class="player-play"
        onClick={toggle}
        disabled={failed()}
        aria-label={playing() ? "Pause" : "Play"}
      >
        {buffering() ? (
          <span class="spinner" />
        ) : playing() ? (
          <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor">
            <rect x="6" y="5" width="4" height="14" rx="1.2" />
            <rect x="14" y="5" width="4" height="14" rx="1.2" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor">
            <path d="M8 5.5v13a1 1 0 0 0 1.54.84l10-6.5a1 1 0 0 0 0-1.68l-10-6.5A1 1 0 0 0 8 5.5z" />
          </svg>
        )}
      </button>

      <button class="btn-icon" onClick={() => seekBy(-10)} title="Back 10s (←)">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <path d="M11 17l-5-5 5-5M18 17l-5-5 5-5" />
        </svg>
      </button>
      <button class="btn-icon" onClick={() => seekBy(10)} title="Forward 10s (→)">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <path d="M13 17l5-5-5-5M6 17l5-5-5-5" />
        </svg>
      </button>

      <span class="player-time mono">
        {formatTime(props.currentTime)}
        <span class="faint"> / {formatTime(props.duration)}</span>
      </span>

      <div class="spacer" />

      <select
        class="player-speed mono"
        value={String(speed())}
        onChange={(e) => setSpeed(Number(e.currentTarget.value))}
        aria-label="Playback speed"
      >
        {SPEEDS.map((s) => (
          <option value={String(s)}>{s}×</option>
        ))}
      </select>

      <div class="player-volume" title="Volume">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round"
             stroke-linejoin="round">
          <path d="M11 5L6 9H2v6h4l5 4V5z" />
          {volume() > 0.05 && <path d="M15.5 8.5a5 5 0 0 1 0 7" />}
        </svg>
        <input
          type="range" min="0" max="1" step="0.01"
          value={String(volume())}
          onInput={(e) => setVolume(Number(e.currentTarget.value))}
          aria-label="Volume"
        />
      </div>
    </div>
  );
}
