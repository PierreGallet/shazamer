import { For, Show, createResource, createSignal } from "solid-js";
import { api } from "../lib/api";

/** Starred tracks, filterable by the things a DJ actually sorts on. */

const CAMELOT = [
  "1A","2A","3A","4A","5A","6A","7A","8A","9A","10A","11A","12A",
  "1B","2B","3B","4B","5B","6B","7B","8B","9B","10B","11B","12B",
];

export default function Crate() {
  const [query, setQuery] = createSignal("");
  const [camelot, setCamelot] = createSignal("");
  const [bpmMin, setBpmMin] = createSignal("");
  const [bpmMax, setBpmMax] = createSignal("");
  const [starredOnly, setStarredOnly] = createSignal(true);

  const [results, { refetch }] = createResource(
    () => ({
      q: query(),
      camelot: camelot(),
      bpmMin: bpmMin() ? Number(bpmMin()) : undefined,
      bpmMax: bpmMax() ? Number(bpmMax()) : undefined,
      starred: starredOnly(),
    }),
    (params) => api.searchLibrary(params),
  );

  async function unstar(key: string, title: string, artist: string) {
    await api.star(key, title, artist);
    await refetch();
  }

  return (
    <div class="wrap">
      <div class="crate-filters panel">
        <input
          class="input input-sm"
          placeholder="Search artist, title or label…"
          value={query()}
          onInput={(e) => setQuery(e.currentTarget.value)}
        />
        <select
          class="input input-sm"
          value={camelot()}
          onChange={(e) => setCamelot(e.currentTarget.value)}
          aria-label="Camelot key"
        >
          <option value="">Any key</option>
          <For each={CAMELOT}>{(k) => <option value={k}>{k}</option>}</For>
        </select>
        <input
          class="input input-sm" type="number" placeholder="BPM min"
          value={bpmMin()} onInput={(e) => setBpmMin(e.currentTarget.value)}
        />
        <input
          class="input input-sm" type="number" placeholder="BPM max"
          value={bpmMax()} onInput={(e) => setBpmMax(e.currentTarget.value)}
        />
        <label class="row tiny muted" style={{ gap: "0.4rem", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={starredOnly()}
            onChange={(e) => setStarredOnly(e.currentTarget.checked)}
          />
          Crate only
        </label>
      </div>

      <Show when={results.loading}><span class="spinner" /></Show>

      <Show
        when={results() && results()!.length > 0}
        fallback={
          <Show when={!results.loading}>
            <div class="empty">
              <div class="empty-title">
                {starredOnly() ? "Nothing starred yet" : "Nothing matches"}
              </div>
              <div class="small">
                {starredOnly()
                  ? "Star a track in any set and it collects here — filterable "
                    + "by BPM and key, which is how you build a run."
                  : "Loosen the filters and try again."}
              </div>
            </div>
          </Show>
        }
      >
        <div class="tracklist" style={{ "margin-top": "1rem" }}>
          <For each={results()!}>
            {(track) => (
              <div class="track-row">
                <span class="track-index mono">♪</span>
                <span class="track-time mono">{track.start_label}</span>
                <div class="track-body">
                  <div class="track-title">{track.title}</div>
                  <div class="track-artist small">
                    {track.artist}
                    <Show when={track.set_title}>
                      <span class="faint"> · in {track.set_title}</span>
                    </Show>
                  </div>
                </div>
                <div class="track-meta">
                  <Show when={track.bpm}>
                    <span class="chip">{track.bpm!.toFixed(0)}</span>
                  </Show>
                  <Show when={track.camelot}>
                    <span class="chip chip-key">{track.camelot}</span>
                  </Show>
                </div>
                <div class="track-actions">
                  <button
                    class="btn-icon on"
                    title="Remove from starred"
                    onClick={() => unstar(track.key, track.title, track.artist)}
                  >
                    <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"
                         stroke="currentColor" stroke-width="2" stroke-linejoin="round">
                      <path d="M12 3l2.9 5.9 6.1.9-4.5 4.4 1.1 6.3L12 17.6 6.4 20.5l1.1-6.3L3 9.8l6.1-.9z" />
                    </svg>
                  </button>
                  <Show when={track.url}>
                    <a class="btn-icon" href={track.url} target="_blank"
                       rel="noopener noreferrer" title="Open on Shazam">
                      <svg viewBox="0 0 24 24" width="15" height="15" fill="none"
                           stroke="currentColor" stroke-width="2"
                           stroke-linecap="round" stroke-linejoin="round">
                        <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                        <path d="M15 3h6v6M10 14L21 3" />
                      </svg>
                    </a>
                  </Show>
                </div>
              </div>
            )}
          </For>
        </div>
      </Show>
    </div>
  );
}
