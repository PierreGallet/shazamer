import { For, Show, createEffect, createSignal, onCleanup } from "solid-js";
import type { Track } from "../lib/api";
import { api, formatTime } from "../lib/api";
import AcquirePanel from "./AcquirePanel";
import PreviewButton from "./PreviewButton";

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
  /** The set these tracks belong to, so a verdict can be recorded. */
  setId?: string;
}

export default function TrackList(props: Props) {
  const [expanded, setExpanded] = createSignal<number | null>(null);
  const [starred, setStarred] = createSignal<Record<string, boolean>>({});
  // Which row is playing its excerpt, so only that one shows a scrubber and
  // starting one stops the rest.
  const [previewKey, setPreviewKey] = createSignal<string | null>(null);

  /**
   * Verdicts on the identifications, by track position.
   *
   * They do not change the tracklist — nothing here can correct Shazam. What
   * they buy is a labelled set of segments, so a heuristic can be measured
   * against real cases instead of guessed at. The one rule this project has
   * that was not a guess came from exactly six of these.
   */
  const [rated, setRated] = createSignal<Record<number, "right" | "wrong">>({});

  createEffect(() => {
    const id = props.setId;
    if (!id) return;
    void api.setFeedback(id).then((got) => {
      const byPosition: Record<number, "right" | "wrong"> = {};
      for (const [position, verdict] of Object.entries(got)) {
        byPosition[Number(position)] = verdict;
      }
      setRated(byPosition);
    }).catch(() => {});
  });

  async function rate(track: Track, verdict: "right" | "wrong",
                      event: MouseEvent) {
    event.stopPropagation();
    const id = props.setId;
    if (!id) return;
    // Clicking the verdict already given takes it back, so a misclick is one
    // click to undo rather than a label that has to be believed.
    const next = rated()[track.index] === verdict ? undefined : verdict;
    setRated({ ...rated(), [track.index]: next as "right" | "wrong" });
    if (next) {
      try {
        await api.rateTrack(id, track.index, next);
      } catch {
        setRated({ ...rated(), [track.index]: undefined as never });
      }
    }
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
                <Show when={track.identified && track.strength}>
                  {/* Every identified track carries its confidence, including
                      the solid ones. Flagging only the shaky ones left the
                      rest ambiguous: an unmarked row could be well-evidenced
                      or simply old, and on a set where three findings in six
                      were invented, knowing which three are solid matters as
                      much as knowing which are not. */}
                  <span
                    class="chip"
                    classList={{
                      "chip-key": track.strength === "strong",
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
                    {track.strength === "weak" ? "unsure"
                      : track.strength === "medium" ? "likely" : "solid"}
                  </span>
                </Show>
              </div>

              <div class="track-actions" onClick={(e) => e.stopPropagation()}>
                <Show when={track.identified}>
                  {/* Right or wrong. Not to fix the tracklist — nothing here
                      can correct Shazam — but so a heuristic can be measured
                      against real cases. Shown only where there is a claim to
                      judge. */}
                  <Show when={props.setId && track.identified}>
                    <button
                      class="btn-icon"
                      classList={{ good: rated()[track.index] === "right" }}
                      title="This identification is right"
                      onClick={(e) => rate(track, "right", e)}
                    >
                      <svg viewBox="0 0 24 24" width="15" height="15" fill="none"
                           stroke="currentColor" stroke-width="2.4"
                           stroke-linecap="round" stroke-linejoin="round">
                        <path d="M4 12.5l5 5L20 6.5" />
                      </svg>
                    </button>
                    <button
                      class="btn-icon"
                      classList={{ bad: rated()[track.index] === "wrong" }}
                      title="This is not the right track"
                      onClick={(e) => rate(track, "wrong", e)}
                    >
                      <svg viewBox="0 0 24 24" width="15" height="15" fill="none"
                           stroke="currentColor" stroke-width="2.4"
                           stroke-linecap="round">
                        <path d="M6 6l12 12M18 6L6 18" />
                      </svg>
                    </button>
                  </Show>

                  <PreviewButton
                    trackKey={track.key}
                    onStart={() => {
                      // The set and the excerpt at once is the one thing that
                      // makes checking a match by ear useless.
                      props.onPreviewStart?.();
                      setPreviewKey(track.key);
                    }}
                    scrub={previewKey() === track.key}
                  />

                  <button
                    class="btn-icon"
                    classList={{ on: isStarred(track) }}
                    title={isStarred(track) ? "Remove from starred" : "Star it"}
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
