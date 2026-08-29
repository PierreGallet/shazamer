import { A } from "@solidjs/router";
import { For, Show, createEffect, createResource, createSignal } from "solid-js";
import ShareSet from "./ShareSet";
import type { LibraryTrack } from "../lib/api";
import { api, formatDuration } from "../lib/api";

/**
 * The library: every set analysed, and the cross-set view that makes it worth
 * keeping — a track appearing in four different sets is the strongest digging
 * signal there is.
 */

interface Props {
  onOpen: (setId: string) => void;
}

export default function Library(props: Props) {
  const [sets, { refetch }] = createResource(() => api.sets(60));
  const [recurring] = createResource(() => api.recurring(2));
  const [deleting, setDeleting] = createSignal<string | null>(null);

  async function remove(setId: string, event: MouseEvent) {
    event.stopPropagation();
    setDeleting(setId);
    try {
      await api.deleteSet(setId);
      await refetch();
    } finally {
      setDeleting(null);
    }
  }

  /**
   * The record whose appearances are being shown.
   *
   * "Showing up across your sets" is the strongest signal this tool
   * produces, and it was a number on a card that led nowhere. The question
   * it raises is always the same — which sets, and at what moment — so
   * clicking one answers exactly that.
   */
  const [showing, setShowing] = createSignal<LibraryTrack | null>(null);
  const [where] = createResource(
    showing, (track) => api.appearances(track.key));

  return (
    <div class="wrap">
      <dialog
        class="picker"
        ref={(el) => {
          // Opened and closed from the signal rather than imperatively at the
          // call site, so Escape and the backdrop close it without leaving
          // the signal pointing at a record nobody is looking at.
          createEffect(() => {
            if (showing()) el.showModal();
            else if (el.open) el.close();
          });
        }}
        onClose={() => setShowing(null)}
      >
        <Show when={showing()}>
          {(track) => (
            <>
              <div class="picker-head">
                <div>
                  <div class="eyebrow">Turns up in</div>
                  <div class="tiny faint">
                    {track().artist} — {track().title}
                  </div>
                </div>
                <button class="btn btn-ghost btn-sm"
                        onClick={() => setShowing(null)}>
                  Close
                </button>
              </div>

              <Show
                when={where()}
                fallback={<div class="picker-wait"><span class="spinner" /></div>}
              >
                <div class="appearances">
                  <For each={where()!}>
                    {(spot) => (
                      <A class="appearance" href={`/sets/${spot.set_id}`}
                         onClick={() => setShowing(null)}>
                        <span class="appearance-title">{spot.set_title}</span>
                        <span class="tiny faint mono">{spot.start_label}</span>
                      </A>
                    )}
                  </For>
                </div>
              </Show>
            </>
          )}
        </Show>
      </dialog>
      <Show when={recurring() && recurring()!.length > 0}>
        <section class="stack" style={{ "margin-bottom": "2.25rem" }}>
          <div class="row">
            <span class="eyebrow">Showing up across your sets</span>
          </div>
          <div class="recurring-grid">
            <For each={recurring()!.slice(0, 12)}>
              {(track) => (
                <button
                  class="recurring-card"
                  onClick={() => setShowing(track)}
                  title="Which sets it turns up in"
                >
                  <div class="recurring-count mono">{track.set_count}×</div>
                  <div class="recurring-body">
                    <div class="recurring-title">{track.title}</div>
                    <div class="tiny muted">{track.artist}</div>
                    <div class="row-wrap" style={{ "margin-top": "0.35rem" }}>
                      <Show when={track.bpm}>
                        <span class="chip">{track.bpm!.toFixed(0)}</span>
                      </Show>
                      <Show when={track.camelot}>
                        <span class="chip chip-key">{track.camelot}</span>
                      </Show>
                      <Show when={track.label}>
                        <span class="tiny faint">{track.label}</span>
                      </Show>
                    </div>
                  </div>
                </button>
              )}
            </For>
          </div>
        </section>
      </Show>

      <section class="stack">
        <div class="row">
          <span class="eyebrow">Analysed sets</span>
          <div class="spacer" />
          <Show when={sets.loading}><span class="spinner" /></Show>
        </div>

        <Show
          when={sets() && sets()!.length > 0}
          fallback={
            <Show when={!sets.loading}>
              <div class="empty">
                <div class="empty-title">No sets yet</div>
                <div class="small">
                  Paste a mix on the Analyse tab and it will land here.
                </div>
              </div>
            </Show>
          }
        >
          <div class="set-list">
            <For each={sets()!}>
              {(item) => (
                <div
                  class="set-card"
                  onClick={() => props.onOpen(item.id)}
                  role="button"
                  tabindex="0"
                  onKeyDown={(e) => {
                    if (e.key === "Enter") props.onOpen(item.id);
                  }}
                >
                  <div class="set-card-body">
                    <div class="set-card-title">{item.title}</div>
                    <div class="row-wrap tiny faint">
                      <span class="mono">
                        {item.identified_count ?? 0}/{item.track_count ?? 0} tracks
                      </span>
                      <span>·</span>
                      <span>{formatDuration(item.duration)}</span>
                      <Show when={item.uploader}>
                        <span>·</span><span>{item.uploader}</span>
                      </Show>
                      <Show when={item.quality}>
                        <span>·</span><span class="mono">{item.quality}</span>
                      </Show>
                      <Show when={item.shared_by}>
                        <span>·</span>
                        <span>shared by {item.shared_by}</span>
                      </Show>
                      <Show when={!item.has_audio}>
                        <span>·</span>
                        <span>
                          {item.source_kind === "legacy"
                            ? "imported — no audio"
                            : "audio cleared"}
                        </span>
                      </Show>
                    </div>
                  </div>

                  {/* Stops the click reaching the card, which would open the
                      set underneath the dialog. */}
                  <div class="set-card-share"
                       onClick={(e) => e.stopPropagation()}>
                    <ShareSet setId={item.id} title={item.title} />
                  </div>

                  <div class="set-card-meta">
                    <Show when={item.stats.coverage != null}>
                      <div
                        class="coverage"
                        title={`${Math.round((item.stats.coverage ?? 0) * 100)}% of the set identified`}
                      >
                        <div
                          class="coverage-fill"
                          style={{ width: `${(item.stats.coverage ?? 0) * 100}%` }}
                        />
                      </div>
                    </Show>
                    <span class="tiny faint mono">
                      {item.created_at.slice(0, 10)}
                    </span>
                    <button
                      class="btn-icon"
                      title="Delete this set"
                      disabled={deleting() === item.id}
                      onClick={(e) => remove(item.id, e)}
                    >
                      <svg viewBox="0 0 24 24" width="15" height="15" fill="none"
                           stroke="currentColor" stroke-width="2"
                           stroke-linecap="round" stroke-linejoin="round">
                        <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
                      </svg>
                    </button>
                  </div>
                </div>
              )}
            </For>
          </div>
        </Show>
      </section>
    </div>
  );
}
