import { Show, createSignal, onCleanup } from "solid-js";
import { api, formatTime } from "../lib/api";

/**
 * Play a ~30 second excerpt of the record, with a scrubber.
 *
 * Extracted from the tracklist so the starred screen can use the same thing.
 * Duplicating it would have been two implementations of the awkward part —
 * playback has to start synchronously inside the click, because an `await`
 * before `play()` ends the user gesture and Chrome then declines to start
 * with no error the page can see.
 *
 * One <audio> element per instance, and each stops the others through
 * `onStart`: two excerpts at once is the single thing that makes checking a
 * match by ear useless.
 */

interface Props {
  trackKey: string;
  /** Called just before playback begins — to pause a set, or another row. */
  onStart?: () => void;
  /** Show the scrubber inline. Off where the row has no space for it. */
  scrub?: boolean;
}

export default function PreviewButton(props: Props) {
  const [playing, setPlaying] = createSignal(false);
  const [busy, setBusy] = createSignal(false);
  const [missing, setMissing] = createSignal(false);
  const [at, setAt] = createSignal(0);
  const [span, setSpan] = createSignal(0);
  let audio: HTMLAudioElement | undefined;

  onCleanup(() => audio?.pause());

  function stop() {
    audio?.pause();
    setPlaying(false);
    setAt(0);
    setSpan(0);
  }

  function toggle(event: MouseEvent) {
    event.stopPropagation();
    if (playing()) {
      stop();
      return;
    }
    props.onStart?.();

    // Not async up to play(). The address is derivable, so the lookup happens
    // server-side behind it and the gesture survives.
    if (!audio) audio = new Audio();
    audio.src = api.trackPreviewUrl(props.trackKey);
    audio.ontimeupdate = () => setAt(audio!.currentTime);
    audio.onloadedmetadata = () => setSpan(audio!.duration || 0);
    audio.onended = () => stop();
    audio.onerror = () => {
      setMissing(true);
      setBusy(false);
      setPlaying(false);
    };

    setBusy(true);
    audio
      .play()
      .then(() => {
        setPlaying(true);
        setBusy(false);
      })
      .catch(() => {
        setMissing(true);
        setBusy(false);
      });
  }

  return (
    <>
      <button
        class="btn-icon"
        classList={{ on: playing(), muted: missing() }}
        disabled={busy()}
        title={
          missing()
            ? "No excerpt of this record is available"
            : playing()
              ? "Stop"
              : "Hear the record, to check the match"
        }
        onClick={toggle}
      >
        <Show
          when={playing()}
          fallback={
            <svg viewBox="0 0 24 24" width="15" height="15"
                 fill="currentColor" aria-hidden="true">
              <path d="M8 5.5v13l10-6.5z" />
            </svg>
          }
        >
          <svg viewBox="0 0 24 24" width="15" height="15"
               fill="currentColor" aria-hidden="true">
            <rect x="7" y="5.5" width="3.4" height="13" rx="1" />
            <rect x="13.6" y="5.5" width="3.4" height="13" rx="1" />
          </svg>
        </Show>
      </button>

      <Show when={props.scrub !== false && playing() && span() > 0}>
        <span class="preview-scrub">
          <input
            type="range"
            min="0"
            max={span()}
            step="0.1"
            value={at()}
            aria-label="Position in the excerpt"
            onInput={(e) => {
              if (audio) {
                audio.currentTime = Number(e.currentTarget.value);
                setAt(audio.currentTime);
              }
            }}
          />
          <span class="tiny faint mono">
            {formatTime(at())} / {formatTime(span())}
          </span>
        </span>
      </Show>
    </>
  );
}
