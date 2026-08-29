import { For, Show, createEffect, createSignal, onCleanup } from "solid-js";
import type { Track } from "../lib/api";
import { api, formatTime } from "../lib/api";
import AcquirePanel from "./AcquirePanel";

/**
 * The tracklist, following playback.
 *
 * Unidentified segments keep their row. Hiding them would misrepresent the
 * set — and they are where the interesting records hide, so they get a
 * "retry" affordance rather than being swept out of sight.
 */

interface Props {
  tracks: Track[];
  activeIndex: number | null;
  onSeek: (time: number) => void;
  onSelect: (index: number) => void;
  /** Called just before a reference excerpt starts, so the set can be paused. */
  onPreviewStart?: () => void;
}

export default function TrackList(props: Props) {
  const [expanded, setExpanded] = createSignal<number | null>(null);
  const [starred, setStarred] = createSignal<Record<string, boolean>>({});

  /**
   * Checking a match by ear.
   *
   * A tracklist is a set of claims. The only way to check one is to hear the
   * record next to the moment it was claimed for, which is why this plays the
   * *reference* — the set itself is already a click away on the waveform.
   *
   * One element, reused. Several would let two excerpts overlap, and the one
   * thing this must never do is make two records play at once while you are
   * trying to decide whether they are the same record.
   */
  const [previewKey, setPreviewKey] = createSignal<string | null>(null);
  const [previewBusy, setPreviewBusy] = createSignal<string | null>(null);
  const [noPreview, setNoPreview] = createSignal<Record<string, boolean>>({});
  // Position and length of the excerpt playing, so it can be scrubbed. Read
  // off the element rather than counted with a timer: a timer drifts, and it
  // keeps running when the audio stalls on a slow connection.
  const [at, setAt] = createSignal(0);
  const [span, setSpan] = createSignal(0);
  let previewAudio: HTMLAudioElement | undefined;

  onCleanup(() => previewAudio?.pause());

  function stopPreview() {
    previewAudio?.pause();
    setPreviewKey(null);
    setAt(0);
    setSpan(0);
  }

  function seekPreview(seconds: number) {
    if (previewAudio) {
      previewAudio.currentTime = seconds;
      setAt(seconds);
    }
  }

  function togglePreview(track: Track, event: MouseEvent) {
    event.stopPropagation();
    if (previewKey() === track.key) {
      stopPreview();
      return;
    }
    stopPreview();
    props.onPreviewStart?.();

    // Deliberately not async up to play(). An `await` here — even a fast one
    // asking the server where the audio is — ends the user gesture, and
    // Chrome then declines to start playback with no error the page can see.
    // The address is predictable, so nothing needs asking first: the server
    // looks the excerpt up behind it and answers 404 when there is none.
    if (!previewAudio) previewAudio = new Audio();
    previewAudio.src = api.trackPreviewUrl(track.key);
    previewAudio.ontimeupdate = () => setAt(previewAudio!.currentTime);
    previewAudio.onloadedmetadata = () => setSpan(previewAudio!.duration || 0);
    previewAudio.onended = () => stopPreview();
    previewAudio.onerror = () => {
      setNoPreview({ ...noPreview(), [track.key]: true });
      setPreviewBusy(null);
      setPreviewKey(null);
    };

    setPreviewBusy(track.key);
    previewAudio
      .play()
      .then(() => {
        setPreviewKey(track.key);
        setPreviewBusy(null);
      })
      .catch(() => {
        setNoPreview({ ...noPreview(), [track.key]: true });
        setPreviewBusy(null);
      });
  }

  /**
   * Keep the playing row in view.
   *
   * The waveform above is sticky, so scrolling the page to follow playback
   * never takes it off screen — which is why this scrolls the page rather
   * than a nested container. A nested scroller was tried first and made
   * things worse: most sets are short enough that it only added a second
   * scrollbar for a hundred pixels of travel.
   *
   * Only fires when the row is actually out of view, so it never fights a
   * user who is reading somewhere else in the list.
   */
  createEffect(() => {
    const index = props.activeIndex;
    if (index === null) return;

    // Deferred on purpose: the effect fires inside Solid's reactive update,
    // while the DOM for this change is still settling, and a scroll requested
    // there is measured against stale geometry.
    //
    // setTimeout rather than requestAnimationFrame — rAF does not fire while
    // the tab is in the background, so a seek made just before switching away
    // would leave the scroll queued indefinitely.
    const timer = window.setTimeout(() => {
      const row = document.querySelector<HTMLElement>(
        `[data-track-index="${index}"]`,
      );
      if (!row) return;

      const box = row.getBoundingClientRect();
      const topLimit = 260;                       // below the sticky waveform
      const bottomLimit = window.innerHeight - 24;
      if (box.top < topLimit || box.bottom > bottomLimit) {
        // Instant, not smooth. Chrome suppresses smooth-scroll animations
        // whenever the tab is not the foreground one, and a suppressed smooth
        // scroll does not fall back to jumping — it simply does nothing, so
        // the list would silently stop following playback. A snap is also
        // less distracting than a half-second glide on every track change.
        row.scrollIntoView({ behavior: "auto", block: "center" });
      }
    }, 0);
    onCleanup(() => window.clearTimeout(timer));
  });

  const isStarred = (track: Track) =>
    starred()[track.key] ?? track.starred ?? false;

  async function toggleStar(track: Track, event: MouseEvent) {
    event.stopPropagation();
    if (!track.key) return;
    const optimistic = !isStarred(track);
    setStarred({ ...starred(), [track.key]: optimistic });
    try {
      const result = await api.star(track.key, track.title, track.artist);
      setStarred({ ...starred(), [track.key]: result.starred });
    } catch {
      setStarred({ ...starred(), [track.key]: !optimistic });
    }
  }

  return (
    <div class="tracklist">
      <For each={props.tracks}>
        {(track, i) => (
          <>
            <div
              data-track-index={i()}
              class="track-row"
              classList={{
                active: props.activeIndex === i(),
                unidentified: !track.identified,
              }}
              onClick={() => {
                props.onSeek(track.start);
                props.onSelect(i());
              }}
              role="button"
              tabindex="0"
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  props.onSeek(track.start);
                  props.onSelect(i());
                }
              }}
            >
              <span class="track-index mono">
                {track.identified ? track.index : "—"}
              </span>
              <span class="track-time mono">{track.start_label}</span>

              <div class="track-body">
                <div class="track-title">
                  <Show
                    when={track.identified}
                    fallback={<span class="track-unknown">ID ?</span>}
                  >
                    {track.title}
                  </Show>
                </div>
                <div class="track-artist small">
                  <Show
                    when={track.identified}
                    fallback={
                      <>
                        {formatTime(track.duration)} unidentified — often a dub or
                        an unreleased edit
                      </>
                    }
                  >
                    {track.artist}
                    <Show when={track.label}>
                      <span class="faint"> · {track.label}</span>
                    </Show>
                    <Show when={track.catalog_number}>
                      {/* The catalogue number is what you search a shop or
                          Discogs with — more use than the label alone. */}
                      <span class="faint mono catalog"> {track.catalog_number}</span>
                    </Show>
                    <Show when={track.year}>
                      <span class="faint"> · {track.year}</span>
                    </Show>
                  </Show>
                </div>
              </div>

              <div class="track-meta">
                <Show when={track.bpm}>
                  <span class="chip" title="Tempo">{track.bpm!.toFixed(0)}</span>
                </Show>
                <Show when={track.camelot}>
                  <span class="chip chip-key" title={track.musical_key ?? ""}>
                    {track.camelot}
                  </span>
                </Show>
                <Show when={track.identified && track.strength &&
                            track.strength !== "strong"}>
                  {/* Only the shaky ones are flagged. Marking every track
                      would turn the signal into wallpaper — what a digger
                      needs to see is which findings not to trust. */}
                  <span
                    class="chip"
                    classList={{
                      "chip-warn": track.strength === "medium",
                      "chip-crit": track.strength === "weak",
                    }}
                    title={
                      track.strength === "weak"
                        ? `Thin evidence: ${track.votes} probe(s) named this, ` +
                          `out of ${track.probes} across the segment. Often a ` +
                          "short track, or one sitting across a transition."
                        : `${track.votes} probes named this. Probes that came ` +
                          "back empty are not counted against it — fingerprinting " +
                          "fails on breakdowns and unreleased passages."
                    }
                  >
                    {track.strength === "weak" ? "unsure" : "likely"}
                  </span>
                </Show>
              </div>

              <div class="track-actions" onClick={(e) => e.stopPropagation()}>
                <Show when={track.identified}>
                  <button
                    class="btn-icon"
                    classList={{
                      on: previewKey() === track.key,
                      muted: noPreview()[track.key],
                    }}
                    disabled={previewBusy() === track.key}
                    title={
                      noPreview()[track.key]
                        ? "No excerpt of this record is available"
                        : previewKey() === track.key
                          ? "Stop"
                          : "Hear the record, to check the match"
                    }
                    onClick={(e) => togglePreview(track, e)}
                  >
                    <Show
                      when={previewKey() === track.key}
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

                  {/* A scrubber for the excerpt, in the row rather than in a
                      player elsewhere: what it is for is comparing this
                      thirty seconds against the moment above it, and a
                      control that lives somewhere else breaks that. A range
                      input rather than a drawn bar — dragging, keyboard and
                      touch all come with it. */}
                  <Show when={previewKey() === track.key && span() > 0}>
                    <span class="preview-scrub">
                      <input
                        type="range"
                        min="0"
                        max={span()}
                        step="0.1"
                        value={at()}
                        aria-label="Position in the excerpt"
                        onInput={(e) =>
                          seekPreview(Number(e.currentTarget.value))}
                      />
                      <span class="tiny faint mono">
                        {formatTime(at())} / {formatTime(span())}
                      </span>
                    </span>
                  </Show>

                  <button
                    class="btn-icon"
                    classList={{ on: isStarred(track) }}
                    title={isStarred(track) ? "Remove from crate" : "Add to crate"}
                    onClick={(e) => toggleStar(track, e)}
                  >
                    <svg viewBox="0 0 24 24" width="15" height="15"
                         fill={isStarred(track) ? "currentColor" : "none"}
                         stroke="currentColor" stroke-width="2"
                         stroke-linejoin="round">
                      <path d="M12 3l2.9 5.9 6.1.9-4.5 4.4 1.1 6.3L12 17.6 6.4 20.5l1.1-6.3L3 9.8l6.1-.9z" />
                    </svg>
                  </button>
                  <button
                    class="btn-icon"
                    classList={{ on: expanded() === i() }}
                    title="Where to get it"
                    onClick={() => setExpanded(expanded() === i() ? null : i())}
                  >
                    <svg viewBox="0 0 24 24" width="15" height="15" fill="none"
                         stroke="currentColor" stroke-width="2"
                         stroke-linecap="round" stroke-linejoin="round">
                      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                      <path d="M7 10l5 5 5-5M12 15V3" />
                    </svg>
                  </button>
                  <Show when={track.url}>
                    <a
                      class="btn-icon"
                      href={track.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      title="Open on Shazam"
                    >
                      <svg viewBox="0 0 24 24" width="15" height="15" fill="none"
                           stroke="currentColor" stroke-width="2"
                           stroke-linecap="round" stroke-linejoin="round">
                        <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                        <path d="M15 3h6v6M10 14L21 3" />
                      </svg>
                    </a>
                  </Show>
                </Show>
              </div>
            </div>

            <Show when={expanded() === i() && track.identified}>
              <AcquirePanel track={track} />
            </Show>
          </>
        )}
      </For>
    </div>
  );
}
